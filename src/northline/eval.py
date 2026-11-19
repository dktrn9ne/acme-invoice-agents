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
    }


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


def render(rows: list[dict], summary: dict, mode: str) -> None:
    console = Console()
    table = Table(title=f"Northline eval · {mode}", header_style="bold")
    table.add_column("Invoice")
    table.add_column("Decision")
    table.add_column("Expected")
    table.add_column("Match")
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
        table.add_row(
            row["file"],
            row["decision"] or "—",
            row["expected_decision"],
            "yes" if row["decision_ok"] and row["flags_ok"] else "no",
            "; ".join(problems) or "—",
        )
    console.print(table)
    console.print(
        f"decisions {summary['decisions_correct']}/{summary['documents']} "
        f"({summary['decision_accuracy']:.0%})   "
        f"flag precision {summary['flag_precision']:.0%}   "
        f"recall {summary['flag_recall']:.0%}   "
        f"f1 {summary['flag_f1']:.0%}   "
        f"extraction {'ok' if summary['extraction_ok'] else 'MISMATCH'}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score Northline against the golden invoice set.")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    live = bool(args.live)
    try:
        rows, summary = run_eval(load_expectations(), live=live)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2 if "XAI_API_KEY" in str(exc) else 1
    render(rows, summary, "live" if live else "offline")
    if live:
        # Live is a canary. Completing the set is success; the table is the result.
        return 0
    perfect = (
        summary["decision_accuracy"] == 1.0
        and summary["flag_f1"] == 1.0
        and summary["extraction_ok"]
    )
    return 0 if perfect else 1


if __name__ == "__main__":
    raise SystemExit(main())
