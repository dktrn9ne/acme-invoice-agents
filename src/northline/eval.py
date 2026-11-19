"""Score the desk against eval/expectations.json.

Offline is the contract: decision accuracy and flag precision/recall must be perfect.
Live uses the same file and prints the delta. A missing key is an error, not a zero.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from northline.config import ROOT, resolve_settings
from northline.db import connect
from northline.runner import memory_saver, run_path
from northline.seed import seed

EXPECTATIONS = ROOT / "eval" / "expectations.json"


def load_expectations(path: Path | None = None) -> dict:
    return json.loads((path or EXPECTATIONS).read_text(encoding="utf-8"))


def score_file(expected: dict, result: dict) -> dict:
    got_decision = result.get("final_decision")
    got_flags = sorted({flag["code"] for flag in result.get("flags") or []})
    want_flags = sorted(set(expected["flags"]))
    invoice = result.get("invoice") or {}
    return {
        "file": expected["file"],
        "decision": got_decision,
        "expected_decision": expected["decision"],
        "decision_ok": got_decision == expected["decision"],
        "flags": got_flags,
        "expected_flags": want_flags,
        "flags_ok": got_flags == want_flags,
        "missing_flags": sorted(set(want_flags) - set(got_flags)),
        "extra_flags": sorted(set(got_flags) - set(want_flags)),
        "invoice_number": invoice.get("invoice_number"),
        "expected_number": expected["invoice_number"],
        "number_ok": invoice.get("invoice_number") == expected["invoice_number"],
        "total": invoice.get("total"),
        "expected_total": expected["total"],
        "total_ok": invoice.get("total") == expected["total"],
        "vendor": invoice.get("vendor_name"),
        "expected_vendor": expected["vendor"],
        "vendor_ok": (invoice.get("vendor_name") or "") == (expected["vendor"] or ""),
        "latency_s": (result.get("latency_ms") or 0) / 1000,
        "input_tokens": _sum_tokens(result.get("llm_calls") or [], "input_tokens"),
        "output_tokens": _sum_tokens(result.get("llm_calls") or [], "output_tokens"),
    }


def _sum_tokens(calls: list[dict], key: str) -> int | None:
    values = [call.get(key) for call in calls if call.get(key) is not None]
    if not values:
        return None
    return int(sum(values))


def decision_exit(summary: dict, *, live: bool, min_accuracy: float | None) -> int:
    """Offline must be perfect. Live exits 0 after a completed run unless --min-accuracy is set."""
    if min_accuracy is not None:
        return 0 if summary["decision_accuracy"] + 1e-12 >= min_accuracy else 1
    if live:
        return 0
    perfect = summary["decision_accuracy"] == 1.0 and summary["flag_f1"] == 1.0 and summary["extraction_ok"]
    return 0 if perfect else 1


def aggregate(rows: list[dict]) -> dict:
    true_positive = false_positive = false_negative = 0
    for row in rows:
        got = set(row["flags"])
        want = set(row["expected_flags"])
        true_positive += len(got & want)
        false_positive += len(got - want)
        false_negative += len(want - got)
    precision = true_positive / (true_positive + false_positive) if (true_positive + false_positive) else 1.0
    recall = true_positive / (true_positive + false_negative) if (true_positive + false_negative) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 1.0
    decisions = sum(1 for row in rows if row["decision_ok"])
    return {
        "documents": len(rows),
        "decisions_correct": decisions,
        "decision_accuracy": decisions / len(rows) if rows else 0.0,
        "flag_precision": precision,
        "flag_recall": recall,
        "flag_f1": f1,
        "flag_true_positive": true_positive,
        "flag_false_positive": false_positive,
        "flag_false_negative": false_negative,
        "extraction_ok": all(row["number_ok"] and row["total_ok"] and row["vendor_ok"] for row in rows),
    }


def run_eval(expectations: dict, *, live: bool = False) -> tuple[list[dict], dict]:
    settings = resolve_settings(offline=not live, live=live, log_path=ROOT / "logs" / ("eval-live.jsonl" if live else "eval-offline.jsonl"))
    rows = []
    folder = ROOT / "data" / "invoices"
    for expected in expectations["invoices"]:
        db_path = settings.db_path.parent / f".eval-{expected['file']}.db"
        # A private ledger per document so twins are not punished for sharing an invoice number.
        local = resolve_settings(
            offline=not live,
            live=live,
            db_path=db_path,
            log_path=settings.log_path,
        )
        conn = connect(db_path)
        try:
            seed(conn)
            result = run_path(
                folder / expected["file"],
                local,
                conn=conn,
                checkpointer=memory_saver(),
            )
        finally:
            conn.close()
            db_path.unlink(missing_ok=True)
        rows.append(score_file(expected, result))
    return rows, aggregate(rows)


def render(rows: list[dict], summary: dict, mode: str, *, min_accuracy: float | None, exit_code: int) -> None:
    console = Console()
    table = Table(title=f"Northline eval · {mode}", header_style="bold")
    table.add_column("Invoice")
    table.add_column("Decision")
    table.add_column("Expected")
    table.add_column("Match")
    table.add_column("Latency", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Flag errors")
    for row in rows:
        problems = []
        if row["missing_flags"]:
            problems.append("missing " + ",".join(row["missing_flags"]))
        if row["extra_flags"]:
            problems.append("extra " + ",".join(row["extra_flags"]))
        if not row["total_ok"]:
            problems.append(f"total {row['total']}!={row['expected_total']}")
        if not row["vendor_ok"]:
            problems.append("vendor")
        tokens = "—"
        if row.get("input_tokens") is not None or row.get("output_tokens") is not None:
            tokens = f"{row.get('input_tokens') or 0}/{row.get('output_tokens') or 0}"
        table.add_row(
            row["file"],
            row["decision"] or "—",
            row["expected_decision"],
            "yes" if row["decision_ok"] and row["flags_ok"] else "no",
            f"{row.get('latency_s', 0):.2f}s",
            tokens,
            "; ".join(problems) or "—",
        )
    console.print(table)
    latency = _latency_line(rows)
    console.print(
        f"decisions {summary['decisions_correct']}/{summary['documents']} "
        f"({summary['decision_accuracy']:.0%})   "
        f"flag precision {summary['flag_precision']:.0%}   "
        f"recall {summary['flag_recall']:.0%}   "
        f"f1 {summary['flag_f1']:.0%}   "
        f"extraction {'ok' if summary['extraction_ok'] else 'MISMATCH'}"
    )
    console.print(latency)
    if mode == "live":
        if min_accuracy is None:
            console.print(
                "Live canary finished. Exit 0 because no --min-accuracy was set. "
                "Pass --min-accuracy 1 to fail anything short of a perfect decision score."
            )
        elif exit_code == 0:
            console.print(f"Live canary met --min-accuracy {min_accuracy:.0%}. Exit 0.")
        else:
            console.print(
                f"Live canary is below --min-accuracy {min_accuracy:.0%} "
                f"({summary['decision_accuracy']:.0%}). Exit 1."
            )


def _latency_line(rows: list[dict]) -> str:
    if not rows:
        return "no invoices"
    slowest = max(rows, key=lambda row: row.get("latency_s") or 0)
    mean = sum(row.get("latency_s") or 0 for row in rows) / len(rows)
    incoming = [row["input_tokens"] for row in rows if row.get("input_tokens") is not None]
    outgoing = [row["output_tokens"] for row in rows if row.get("output_tokens") is not None]
    if incoming or outgoing:
        tokens = f"tokens in {sum(incoming)} out {sum(outgoing)}"
    else:
        tokens = "tokens unavailable (no usage metadata on this run)"
    return (
        f"mean {mean:.2f}s/invoice  max {slowest.get('latency_s', 0):.2f}s "
        f"({slowest['file']})  {tokens}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score Northline against the golden invoice set.")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument(
        "--min-accuracy",
        type=float,
        default=None,
        help="Fail (exit 1) when decision accuracy is below this fraction, e.g. 1 or 0.95. "
        "Live runs exit 0 after a completed canary when this is omitted.",
    )
    args = parser.parse_args(argv)
    live = bool(args.live)
    if args.min_accuracy is not None and not 0 <= args.min_accuracy <= 1:
        print("error: --min-accuracy must be between 0 and 1", file=sys.stderr)
        return 2
    try:
        rows, summary = run_eval(load_expectations(), live=live)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2 if "XAI_API_KEY" in str(exc) else 1
    code = decision_exit(summary, live=live, min_accuracy=args.min_accuracy)
    render(rows, summary, "live" if live else "offline", min_accuracy=args.min_accuracy, exit_code=code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
