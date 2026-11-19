"""Durable side effects: payments, the review queue, the audit trail, run history."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

from northline.db import dumps, utcnow
from northline.models import Invoice
from northline.payment import mock_payment


def record_payment(conn: sqlite3.Connection, invoice: Invoice, run_id: str) -> dict:
    number = invoice.invoice_number or ""
    vendor = (invoice.vendor_name or "").strip()
    currency = (invoice.currency or "USD").upper()
    amount = invoice.total if invoice.total is not None else Decimal("0")
    if currency != "USD":
        return {"status": "blocked", "reason": "currency", "currency": currency}
    if amount <= 0:
        return {"status": "blocked", "reason": "non_positive_amount", "amount": str(amount)}
    prior = conn.execute(
        """
        SELECT confirmation, amount, currency, run_id FROM payments
        WHERE invoice_number = ? AND status = 'success'
        ORDER BY id DESC LIMIT 1
        """,
        (number,),
    ).fetchone()
    if prior:
        return {
            "status": "blocked",
            "reason": "duplicate_payment",
            "confirmation": prior["confirmation"],
            "amount": prior["amount"],
        }
    mock_payment(vendor, f"{amount:.2f}")
    confirmation = f"NL-{number}-{run_id.replace('-', '')[:8]}"
    conn.execute(
        """
        INSERT INTO payments (invoice_number, vendor, amount, currency, source_path, run_id, status, confirmation, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'success', ?, ?)
        """,
        (number, vendor, f"{amount:.2f}", currency, invoice.source_path, run_id, confirmation, utcnow()),
    )
    conn.commit()
    return {
        "status": "success",
        "confirmation": confirmation,
        "amount": f"{amount:.2f}",
        "currency": currency,
        "vendor": vendor,
    }


def enqueue(conn: sqlite3.Connection, invoice: Invoice, run_id: str, thread_id: str, reasons: list[dict], source_name: str) -> None:
    conn.execute(
        """
        INSERT INTO review_queue (
            run_id, thread_id, invoice_number, vendor, amount, currency,
            source_path, source_name, reasons, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        ON CONFLICT(run_id) DO UPDATE SET
            reasons = excluded.reasons,
            status = 'pending',
            amount = excluded.amount
        """,
        (
            run_id,
            thread_id,
            invoice.invoice_number,
            invoice.vendor_name,
            None if invoice.total is None else f"{invoice.total:.2f}",
            invoice.currency,
            invoice.source_path,
            source_name,
            dumps(reasons),
            utcnow(),
        ),
    )
    conn.commit()


def save_events(conn: sqlite3.Connection, events: list[dict]) -> None:
    conn.executemany(
        """
        INSERT INTO audit_events (run_id, thread_id, invoice_number, source_path, agent, stage, message, payload, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                event.get("run_id"),
                event.get("thread_id"),
                event.get("invoice_number"),
                event.get("source_path"),
                event.get("agent"),
                event.get("stage"),
                event.get("message"),
                dumps(event.get("payload") or {}),
                event.get("ts") or utcnow(),
            )
            for event in events
        ],
    )
    conn.commit()


def save_run(conn: sqlite3.Connection, result: dict) -> None:
    invoice = result.get("invoice") or {}
    conn.execute(
        """
        INSERT INTO runs (
            run_id, thread_id, source_path, source_name, mode, invoice_number,
            vendor, currency, total, decision, flags_json, result_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            decision = excluded.decision,
            flags_json = excluded.flags_json,
            result_json = excluded.result_json,
            total = excluded.total,
            vendor = excluded.vendor,
            invoice_number = excluded.invoice_number
        """,
        (
            result["run_id"],
            result.get("thread_id"),
            result.get("source_path"),
            result.get("source_name"),
            result.get("mode"),
            invoice.get("invoice_number"),
            invoice.get("vendor_name"),
            invoice.get("currency"),
            invoice.get("total"),
            result.get("final_decision"),
            dumps(result.get("flags") or []),
            dumps(_public_result(result)),
            utcnow(),
        ),
    )
    conn.commit()


def _public_result(result: dict) -> dict:
    skip = {"raw_text"}
    invoice = dict(result.get("invoice") or {})
    invoice.pop("raw_text", None)
    return {
        "run_id": result.get("run_id"),
        "thread_id": result.get("thread_id"),
        "source_name": result.get("source_name"),
        "source_path": result.get("source_path"),
        "mode": result.get("mode"),
        "final_decision": result.get("final_decision"),
        "policy_decision": result.get("policy_decision"),
        "policy_rationale": result.get("policy_rationale"),
        "draft_decision": result.get("draft_decision"),
        "draft_rationale": result.get("draft_rationale"),
        "critique_verdict": result.get("critique_verdict"),
        "critique_notes": result.get("critique_notes"),
        "critique_attempts": result.get("critique_attempts"),
        "extract_attempts": result.get("extract_attempts"),
        "flags": result.get("flags") or [],
        "payment": result.get("payment") or {},
        "events": result.get("events") or [],
        "invoice": {key: value for key, value in invoice.items() if key not in skip},
        "paused": bool(result.get("paused")),
    }


def mark_review(conn: sqlite3.Connection, run_id: str, status: str, note: str = "") -> None:
    conn.execute(
        """
        UPDATE review_queue
        SET status = ?, reviewer_note = ?, resolved_at = ?
        WHERE run_id = ?
        """,
        (status, note, utcnow(), run_id),
    )
    conn.commit()


def resolve_review(conn: sqlite3.Connection, run_id: str, action: str, note: str) -> dict:
    """A person clears or refuses a held invoice. Approval pays through the same port."""
    row = conn.execute("SELECT * FROM review_queue WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(f"No review item for {run_id}")
    if row["status"] != "pending":
        raise RuntimeError(f"Review {run_id} is already {row['status']}")
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        raise KeyError(f"No run {run_id}")
    stored = json.loads(run["result_json"])
    invoice = Invoice.model_validate(stored["invoice"])
    if action == "approve":
        payment = record_payment(conn, invoice, run_id)
        if payment["status"] != "success":
            decision = "rejected"
            message = f"Reviewer approved, but payment was blocked ({payment.get('reason')})."
        else:
            decision = "paid"
            message = f"Reviewer approved. {payment['confirmation']}."
    elif action == "reject":
        payment = {"status": "not_paid", "reason": "reviewer_rejected"}
        decision = "rejected"
        message = "Reviewer rejected the invoice."
    else:
        raise ValueError("action must be approve or reject")
    now = utcnow()
    conn.execute(
        """
        UPDATE review_queue
        SET status = ?, reviewer_note = ?, resolved_at = ?
        WHERE run_id = ?
        """,
        ("approved" if action == "approve" and decision == "paid" else "rejected", note, now, run_id),
    )
    event = {
        "run_id": run_id,
        "thread_id": row["thread_id"],
        "invoice_number": invoice.invoice_number,
        "source_path": invoice.source_path,
        "agent": "reviewer",
        "stage": "review",
        "message": message + (f" Note: {note}" if note else ""),
        "payload": {"action": action, "payment": payment},
        "ts": now,
    }
    save_events(conn, [event])
    stored["final_decision"] = decision
    stored["payment"] = payment
    stored["events"] = list(stored.get("events") or []) + [event]
    stored["paused"] = False
    conn.execute(
        "UPDATE runs SET decision = ?, result_json = ?, total = ? WHERE run_id = ?",
        (decision, dumps(stored), invoice.total if invoice.total is None else f"{invoice.total:.2f}", run_id),
    )
    conn.commit()
    return {"decision": decision, "payment": payment, "message": message}
