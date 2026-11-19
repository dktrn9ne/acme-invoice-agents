from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

from northline.desk import resolve_review
from northline.models import Critique, Invoice, LineItem, Narrative
from northline.reasoners import ScriptedReasoner
from pathlib import Path

from northline.runner import memory_saver, resume_path, run_path

INVOICES = Path(__file__).resolve().parents[1] / "data" / "invoices"


def _good(path) -> Invoice:
    return Invoice(
        invoice_number="INV-9001",
        vendor_name="Widgets Inc.",
        invoice_date=date(2026, 1, 15),
        due_date=date(2026, 2, 15),
        invoice_date_raw="2026-01-15",
        due_date_raw="2026-02-15",
        lines=[
            LineItem(raw_name="WidgetA", sku="WidgetA", quantity=Decimal("1"), unit_price=Decimal("250.00")),
        ],
        subtotal=Decimal("250.00"),
        tax_amount=Decimal("0.00"),
        total=Decimal("250.00"),
        raw_text="clean",
        source_path=str(path),
        source_format="txt",
    )


def test_extraction_self_corrects_on_schema_failure(conn, settings, tmp_path):
    path = tmp_path / "clean.txt"
    path.write_text("Invoice Number: INV-9001\n")
    good = _good(path)
    calls = {"n": 0}

    def extract(feedback, rules):
        calls["n"] += 1
        if calls["n"] == 1:
            assert feedback == []
            return Invoice(raw_text="bad", source_path=str(path), source_format="txt")
        assert "invoice_number missing" in feedback
        return good

    reasoner = ScriptedReasoner(
        extracts=[extract, extract],
        narratives=[lambda dossier: Narrative(decision=dossier["policy_decision"], rationale="Controls cleared.", concerns=[])],
        critiques=[lambda dossier, narrative: Critique(verdict="accept", notes="ok", recommended_decision=narrative.decision)],
    )
    result = run_path(path, settings, reasoner=reasoner, conn=conn, checkpointer=memory_saver())
    assert calls["n"] == 2
    assert result["extract_attempts"] == 2
    assert result["final_decision"] == "paid"
    assert any(event["stage"] == "correct" for event in result["events"])


def test_critic_loop_rejects_a_too_loose_draft(conn, settings):
    path = INVOICES / "invoice_1002.txt"

    def loose(dossier):
        return Narrative(decision="paid", rationale="Ship it.", concerns=[])

    def tightened(dossier):
        assert "looser" in dossier["critique_notes"]
        return Narrative(
            decision="rejected",
            rationale="Do not pay. Amount exceeds the $10,000 VP scrutiny threshold.",
            concerns=["stock_exceeded"],
        )

    def critic(dossier, narrative):
        if narrative.decision == "paid":
            return Critique(verdict="revise", notes="Draft is looser than the controls allow.", recommended_decision="rejected")
        return Critique(verdict="accept", notes="now consistent", recommended_decision="rejected")

    reasoner = ScriptedReasoner(
        extracts=[lambda feedback, rules: rules],
        narratives=[loose, tightened],
        critiques=[critic, critic],
    )
    result = run_path(path, settings, reasoner=reasoner, conn=conn, checkpointer=memory_saver())
    assert reasoner.narrate_calls == 2
    assert reasoner.critique_calls == 2
    assert result["final_decision"] == "rejected"
    assert result["payment"]["status"] == "not_paid"


def test_clamp_blocks_a_critic_that_rubber_stamps_payment(conn, settings):
    path = INVOICES / "invoice_1003.txt"

    def stamp(dossier):
        return Narrative(decision="paid", rationale="Looks urgent, pay it.", concerns=[])

    def accept(dossier, narrative):
        return Critique(verdict="accept", notes="rubber stamp", recommended_decision="paid")

    reasoner = ScriptedReasoner(
        extracts=[lambda feedback, rules: rules],
        narratives=[stamp, stamp],
        critiques=[accept, accept],
    )
    result = run_path(path, settings, reasoner=reasoner, conn=conn, checkpointer=memory_saver())
    assert result["policy_decision"] == "rejected"
    assert result["draft_decision"] == "paid"
    assert result["critique_attempts"] == 2
    assert result["final_decision"] == "rejected"
    assert result["payment"]["reason"] == "rejected"


def test_false_arithmetic_claim_is_revised_back_to_paid(conn, settings):
    """The live miss on INV-1004: the draft forgot the 8% tax and the model critic allowed it."""
    path = INVOICES / "invoice_1004.json"
    false_claim = "Invoice total 1890 does not match line items (3*250 + 2*500 = 1750)"

    def wrong(dossier):
        return Narrative(decision="needs_review", rationale=false_claim, concerns=["arithmetic"])

    def corrected(dossier):
        assert "line sum" in dossier["critique_notes"]
        assert "140" in dossier["critique_notes"]
        return Narrative(
            decision="paid",
            rationale="Stated total 1890.00 includes tax of 140.00 on a line sum of 1750.00. Pay.",
            concerns=[],
        )

    def accept_stricter(dossier, narrative):
        return Critique(
            verdict="accept",
            notes="Draft decision is stricter than policy but rules specify revise only for looser decisions.",
            recommended_decision=narrative.decision,
        )

    reasoner = ScriptedReasoner(
        extracts=[lambda feedback, rules: rules],
        narratives=[wrong, corrected],
        critiques=[accept_stricter, accept_stricter],
    )
    result = run_path(path, settings, reasoner=reasoner, conn=conn, checkpointer=memory_saver())
    assert result["policy_decision"] == "paid"
    assert result["critique_attempts"] == 2
    assert any(event.get("payload", {}).get("verdict") == "revise" for event in result["events"])
    assert result["final_decision"] == "paid"
    assert result["payment"]["status"] == "success"


def test_repeated_false_arithmetic_falls_back_to_policy(conn, settings):
    path = INVOICES / "invoice_1004.json"
    false_claim = "Invoice total 1890 does not match line items (3*250 + 2*500 = 1750)"

    def wrong(dossier):
        return Narrative(decision="needs_review", rationale=false_claim, concerns=[])

    def accept_stricter(dossier, narrative):
        return Critique(verdict="accept", notes="stricter is fine", recommended_decision="needs_review")

    reasoner = ScriptedReasoner(
        extracts=[lambda feedback, rules: rules],
        narratives=[wrong, wrong],
        critiques=[accept_stricter, accept_stricter],
    )
    result = run_path(path, settings, reasoner=reasoner, conn=conn, checkpointer=memory_saver())
    assert result["final_decision"] == "paid"
    assert any("Kept the policy decision" in event["message"] for event in result["events"])


def test_high_value_clean_invoice_is_paid_and_the_threshold_is_named(conn, settings, tmp_path):
    path = tmp_path / "big.txt"
    path.write_text(
        "\n".join(
            [
                "INVOICE",
                "Vendor: Widgets Inc.",
                "Invoice Number: INV-9002",
                "Date: 2026-01-15",
                "Due Date: 2026-02-15",
                "WidgetA    qty: 15    unit price: $250.00",
                "WidgetB    qty: 10    unit price: $500.00",
                "GadgetX    qty: 5     unit price: $750.00",
                "Subtotal: $12,500.00",
                "Tax (0%): $0.00",
                "Total Amount: $12,500.00",
            ]
        )
    )
    result = run_path(path, settings, conn=conn, checkpointer=memory_saver())
    assert result["final_decision"] == "paid"
    assert [flag["code"] for flag in result["flags"]] == ["high_value"]
    assert "10,000" in result["draft_rationale"]
    assert result["critique_verdict"] == "accept"
    assert result["payment"]["status"] == "success"


def test_human_interrupt_can_release_a_held_invoice(conn, settings):
    held = replace(settings, hitl=True)
    saver = memory_saver()
    first = run_path(
        INVOICES / "invoice_1010.txt",
        held,
        conn=conn,
        checkpointer=saver,
        run_id="thread-1010",
    )
    assert first["paused"] is True
    assert first["final_decision"] == "needs_review"
    pending = conn.execute("SELECT status FROM review_queue WHERE run_id = 'thread-1010'").fetchone()
    assert pending["status"] == "pending"
    second = resume_path(held, "thread-1010", "approve", "Rush premium is on the PO.", conn=conn, checkpointer=saver)
    assert second["final_decision"] == "paid"
    assert second["payment"]["status"] == "success"
    status = conn.execute("SELECT status FROM review_queue WHERE run_id = 'thread-1010'").fetchone()
    assert status["status"] == "approved"


def test_queue_reject_does_not_pay(conn, settings):
    result = run_path(INVOICES / "invoice_1014.xml", settings, conn=conn, checkpointer=memory_saver())
    assert result["final_decision"] == "needs_review"
    resolved = resolve_review(conn, result["run_id"], "reject", "No EUR rail.")
    assert resolved["decision"] == "rejected"
    assert conn.execute("SELECT COUNT(*) AS n FROM payments").fetchone()["n"] == 0


def test_second_copy_cannot_double_pay(conn, settings):
    first = run_path(INVOICES / "invoice_1011.txt", settings, conn=conn, checkpointer=memory_saver())
    assert first["final_decision"] == "paid"
    second = run_path(INVOICES / "invoice_1011.pdf", settings, conn=conn, checkpointer=memory_saver())
    assert second["final_decision"] == "rejected"
    assert "duplicate_payment" in {flag["code"] for flag in second["flags"]}
    assert conn.execute("SELECT COUNT(*) AS n FROM payments WHERE status = 'success'").fetchone()["n"] == 1


def test_sqlite_checkpointer_is_written(conn, settings):
    run_path(INVOICES / "invoice_1001.txt", settings, conn=conn)
    assert settings.checkpoint_path.exists()
    assert settings.checkpoint_path.stat().st_size > 0
