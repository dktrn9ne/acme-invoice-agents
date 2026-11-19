"""Command line. `python main.py --invoice_path=data/invoices/invoice_1001.txt`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from northline.config import ROOT, resolve_settings
from northline.runner import discover, resume_path, run_path

DECISION_STYLE = {
    "paid": "bold #1e6b45",
    "rejected": "bold #8f2d2d",
    "needs_review": "bold #8a5a12",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Northline payables desk.")
    parser.add_argument("--invoice_path", help="Process one invoice file.")
    parser.add_argument("--invoice_dir", help="Process every invoice in a directory.")
    parser.add_argument("--all", action="store_true", help="Process data/invoices.")
    parser.add_argument("--offline", action="store_true", help="Force the rules reasoner.")
    parser.add_argument("--live", action="store_true", help="Force Grok. Requires XAI_API_KEY.")
    parser.add_argument("--db", help="SQLite ledger path.")
    parser.add_argument("--log", help="JSON-lines log path.")
    parser.add_argument("--hitl", action="store_true", help="Interrupt needs-review runs for a person.")
    parser.add_argument("--resume", help="Thread id of a paused run.")
    parser.add_argument("--action", choices=["approve", "reject"], help="Resolution when resuming.")
    parser.add_argument("--note", default="", help="Note stored with a human resolution.")
    args = parser.parse_args(argv)
    try:
        settings = resolve_settings(
            offline=True if args.offline else None,
            live=args.live,
            db_path=Path(args.db) if args.db else None,
            log_path=Path(args.log) if args.log else None,
            hitl=args.hitl,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    console = Console()
    if args.resume:
        if not args.action:
            print("error: --resume requires --action approve or reject", file=sys.stderr)
            return 2
        result = resume_path(settings, args.resume, args.action, args.note)
        _render_one(console, result)
        return 0

    paths = _paths(args)
    if not paths:
        print("error: pass --invoice_path, --invoice_dir, or --all", file=sys.stderr)
        return 2
    results = []
    failed = False
    for path in paths:
        try:
            results.append(run_path(path, settings))
        except Exception as exc:
            failed = True
            console.print(f"[red]Failed {path.name}: {exc.__class__.__name__}: {exc}[/red]")
    if len(results) == 1 and not failed:
        _render_one(console, results[0])
    elif results:
        _render_batch(console, results, settings.mode)
    return 1 if failed else 0


def _paths(args) -> list[Path]:
    chosen = []
    if args.invoice_path:
        chosen.append(Path(args.invoice_path))
    if args.invoice_dir:
        chosen.extend(discover(Path(args.invoice_dir)))
    if args.all:
        chosen.extend(discover(ROOT / "data" / "invoices"))
    # Preserve order, drop duplicates.
    seen = set()
    unique = []
    for path in chosen:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _render_one(console: Console, result: dict) -> None:
    invoice = result.get("invoice") or {}
    decision = result.get("final_decision") or "unknown"
    title = Text.assemble(
        (invoice.get("invoice_number") or result.get("source_name") or "Invoice", "bold"),
        "  ",
        (decision.replace("_", " "), DECISION_STYLE.get(decision, "bold")),
    )
    lines = [
        f"Vendor   {invoice.get('vendor_name') or '—'}",
        f"Total    {invoice.get('total') or '—'} {invoice.get('currency') or ''}".rstrip(),
        f"Due      {invoice.get('due_date') or invoice.get('due_date_raw') or '—'}",
        f"Mode     {result.get('mode')}",
        f"File     {result.get('source_name')}",
    ]
    flags = result.get("flags") or []
    if flags:
        lines.append("")
        for flag in flags:
            lines.append(f"{flag['severity']} · {flag['code']} — {flag['message']}")
    else:
        lines.append("")
        lines.append("No flags.")
    payment = result.get("payment") or {}
    if payment.get("confirmation"):
        lines.append("")
        lines.append(f"Payment  {payment['confirmation']}")
    if result.get("paused"):
        lines.append("")
        lines.append(f"Paused. Resume with --resume {result.get('thread_id')} --action approve|reject")
    rationale = result.get("draft_rationale") or result.get("policy_rationale") or ""
    if rationale:
        lines.append("")
        lines.append(rationale)
    console.print(Panel("\n".join(lines), title=title, border_style="#c45c26"))


def _render_batch(console: Console, results: list[dict], mode: str) -> None:
    table = Table(title=f"Northline batch · {mode}", header_style="bold")
    table.add_column("Invoice")
    table.add_column("Vendor")
    table.add_column("Total", justify="right")
    table.add_column("Decision")
    table.add_column("Flags")
    for result in results:
        invoice = result.get("invoice") or {}
        decision = result.get("final_decision") or ""
        flags = ", ".join(flag["code"] for flag in result.get("flags") or []) or "—"
        table.add_row(
            invoice.get("invoice_number") or result.get("source_name"),
            invoice.get("vendor_name") or "—",
            f"{invoice.get('total') or '—'} {invoice.get('currency') or ''}".rstrip(),
            Text(decision, style=DECISION_STYLE.get(decision, "")),
            flags,
        )
    console.print(table)
    counts: dict[str, int] = {}
    for result in results:
        counts[result.get("final_decision") or "?"] = counts.get(result.get("final_decision") or "?", 0) + 1
    summary = "  ".join(f"{key} {value}" for key, value in sorted(counts.items()))
    console.print(summary)


if __name__ == "__main__":
    raise SystemExit(main())
