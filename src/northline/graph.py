"""LangGraph desk: ingest, self-correct, validate, VP draft, critic, settle."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from northline.critique import enforce_critique, rationale_contradicts
from northline.db import utcnow
from northline.desk import enqueue, mark_review, record_payment
from northline.extract import parse_document
from northline.flags import tighter
from northline.models import Invoice, Narrative, schema_errors
from northline.policy import decide, explain
from northline.reasoners import Reasoner
from northline.validate import Ledger, arithmetic_facts, validate_invoice


class DeskState(TypedDict, total=False):
    run_id: str
    thread_id: str
    source_path: str
    source_name: str
    mode: str
    hitl: bool
    raw_text: str
    source_format: str
    feedback: list[str]
    extract_attempts: int
    invoice: dict
    schema_errors: list[str]
    flags: list[dict]
    policy_decision: str
    policy_rationale: str
    arithmetic: dict
    draft_decision: str
    draft_rationale: str
    draft_concerns: list[str]
    critique_attempts: int
    critique_verdict: str
    critique_notes: str
    final_decision: str
    payment: dict
    events: list[dict]
    paused: bool


def build_graph(reasoner: Reasoner, ledger: Ledger, checkpointer):
    def ingest(state: DeskState) -> dict:
        attempts = int(state.get("extract_attempts") or 0) + 1
        feedback = list(state.get("feedback") or [])
        rules = parse_document(Path(state["source_path"]), state.get("raw_text"))
        invoice = reasoner.extract(state.get("raw_text") or "", state.get("source_format") or "", feedback, rules)
        errors = schema_errors(invoice)
        if errors and attempts >= 3 and not schema_errors(rules):
            rules.extraction_notes.append("Fell back to the rules reader after schema retries.")
            invoice = rules
            errors = []
        events = _events(state)
        events.append(
            _event(
                state,
                "ingestion",
                "ingest",
                f"Read {state.get('source_name')} ({state.get('source_format')}), attempt {attempts}. "
                + ("Schema accepted." if not errors else "Schema rejected: " + "; ".join(errors)),
                errors=errors,
                notes=list(invoice.extraction_notes),
                disagreement=list(invoice.extraction_diff),
                invoice_number=invoice.invoice_number,
            )
        )
        if attempts > 1:
            events.append(
                _event(
                    state,
                    "ingestion",
                    "correct",
                    f"Self-correction pass {attempts}.",
                    feedback=feedback,
                )
            )
        return {
            "invoice": invoice.state(),
            "schema_errors": errors,
            "extract_attempts": attempts,
            "feedback": errors,
            "events": events,
        }

    def validate(state: DeskState) -> dict:
        invoice = Invoice.model_validate(state["invoice"])
        before = len(ledger.trace)
        flags = validate_invoice(invoice, ledger, Path(state["source_path"]))
        trace = ledger.trace[before:]
        payload = [flag.as_dict() for flag in flags]
        decision = decide(flags)
        rationale = explain(decision, payload)
        facts = arithmetic_facts(invoice)
        events = _events(state)
        tools = ", ".join(sorted({item["tool"] for item in trace})) or "none"
        events.append(
            _event(
                state,
                "validation",
                "validate",
                f"Controls recommend {decision}. Tools used: {tools}. "
                f"Arithmetic: line sum {facts['line_sum']}, tax {facts['stated_tax'] or '0.00'}, "
                f"expected total {facts['expected_total']}, stated total {facts['stated_total']}, "
                f"total_matches {facts['total_matches']}.",
                tools=trace,
                flags=[flag["code"] for flag in payload],
                arithmetic=facts,
                invoice_number=invoice.invoice_number,
            )
        )
        return {
            "flags": payload,
            "policy_decision": decision,
            "policy_rationale": rationale,
            "arithmetic": facts,
            "events": events,
        }

    def approve(state: DeskState) -> dict:
        dossier = _dossier(state)
        narrative = reasoner.narrate(dossier)
        events = _events(state)
        events.append(
            _event(
                state,
                "approval",
                "approve",
                f"Draft decision {narrative.decision}. {narrative.rationale}",
                decision=narrative.decision,
                invoice_number=(state.get("invoice") or {}).get("invoice_number"),
            )
        )
        return {
            "draft_decision": narrative.decision,
            "draft_rationale": narrative.rationale,
            "draft_concerns": list(narrative.concerns),
            "events": events,
        }

    def critique(state: DeskState) -> dict:
        dossier = _dossier(state)
        narrative = Narrative(
            decision=state["draft_decision"],
            rationale=state.get("draft_rationale") or "",
            concerns=list(state.get("draft_concerns") or []),
        )
        review = enforce_critique(dossier, narrative, reasoner.critique(dossier, narrative))
        attempts = int(state.get("critique_attempts") or 0) + 1
        events = _events(state)
        events.append(
            _event(
                state,
                "critic",
                "critique",
                f"Critic says {review.verdict}. {review.notes}",
                verdict=review.verdict,
                attempt=attempts,
                invoice_number=(state.get("invoice") or {}).get("invoice_number"),
            )
        )
        update: dict = {
            "critique_attempts": attempts,
            "critique_verdict": review.verdict,
            "critique_notes": review.notes,
            "events": events,
        }
        if review.verdict != "revise" or attempts >= 2:
            final = tighter(state["draft_decision"], state["policy_decision"])
            if rationale_contradicts(state.get("draft_rationale") or "", state.get("arithmetic")):
                final = state["policy_decision"]
                events.append(
                    _event(
                        state,
                        "critic",
                        "critique",
                        "Kept the policy decision. The draft still contradicts the computed arithmetic.",
                        invoice_number=(state.get("invoice") or {}).get("invoice_number"),
                    )
                )
            update["final_decision"] = final
            update["events"] = events
        return update

    def settle(state: DeskState) -> dict:
        decision = state["final_decision"]
        invoice = Invoice.model_validate(state["invoice"])
        human_note = ""
        if decision == "needs_review":
            enqueue(
                ledger.conn,
                invoice,
                state["run_id"],
                state.get("thread_id") or state["run_id"],
                list(state.get("flags") or []),
                state.get("source_name") or "",
            )
        if decision == "needs_review" and state.get("hitl"):
            human = interrupt(
                {
                    "run_id": state["run_id"],
                    "invoice_number": invoice.invoice_number,
                    "vendor": invoice.vendor_name,
                    "total": None if invoice.total is None else str(invoice.total),
                    "flags": [flag["code"] for flag in state.get("flags") or []],
                }
            )
            human_note = str((human or {}).get("note") or "")
            action = (human or {}).get("action")
            if action == "approve":
                decision = "paid"
            else:
                decision = "rejected"
        events = _events(state)
        if decision == "paid":
            payment = record_payment(ledger.conn, invoice, state["run_id"])
            if payment["status"] != "success":
                decision = "rejected"
                events.append(
                    _event(
                        state,
                        "payment",
                        "pay",
                        f"Payment blocked ({payment.get('reason')}).",
                        payment=payment,
                        invoice_number=invoice.invoice_number,
                    )
                )
            else:
                events.append(
                    _event(
                        state,
                        "payment",
                        "pay",
                        f"Paid {payment['amount']} {payment['currency']} to {payment['vendor']} ({payment['confirmation']}).",
                        payment=payment,
                        invoice_number=invoice.invoice_number,
                    )
                )
        elif decision == "needs_review":
            payment = {"status": "not_paid", "reason": "needs_review"}
            events.append(
                _event(
                    state,
                    "payment",
                    "review",
                    "Held for a person. No payment was sent.",
                    invoice_number=invoice.invoice_number,
                )
            )
        else:
            payment = {"status": "not_paid", "reason": "rejected"}
            events.append(
                _event(
                    state,
                    "payment",
                    "pay",
                    "Rejected. No payment was sent. " + (state.get("policy_rationale") or ""),
                    invoice_number=invoice.invoice_number,
                )
            )
        if state.get("hitl") and state.get("final_decision") == "needs_review":
            mark_review(
                ledger.conn,
                state["run_id"],
                "approved" if decision == "paid" else "rejected",
                human_note,
            )
        return {"final_decision": decision, "payment": payment, "events": events, "paused": False}

    graph = StateGraph(DeskState)
    graph.add_node("ingest", ingest)
    graph.add_node("validate", validate)
    graph.add_node("approve", approve)
    graph.add_node("critique", critique)
    graph.add_node("settle", settle)
    graph.set_entry_point("ingest")
    graph.add_conditional_edges("ingest", _route_ingest, {"ingest": "ingest", "validate": "validate"})
    graph.add_edge("validate", "approve")
    graph.add_edge("approve", "critique")
    graph.add_conditional_edges("critique", _route_critique, {"approve": "approve", "settle": "settle"})
    graph.add_edge("settle", END)
    return graph.compile(checkpointer=checkpointer)


def _route_ingest(state: DeskState) -> str:
    if state.get("schema_errors") and int(state.get("extract_attempts") or 0) < 3:
        return "ingest"
    return "validate"


def _route_critique(state: DeskState) -> str:
    if state.get("critique_verdict") == "revise" and int(state.get("critique_attempts") or 0) < 2:
        return "approve"
    return "settle"


def _events(state: DeskState) -> list[dict]:
    return list(state.get("events") or [])


def _event(state: DeskState, agent: str, stage: str, message: str, **payload) -> dict:
    invoice = state.get("invoice") or {}
    return {
        "run_id": state.get("run_id"),
        "thread_id": state.get("thread_id"),
        "invoice_number": payload.pop("invoice_number", None) or invoice.get("invoice_number"),
        "source_path": state.get("source_path"),
        "agent": agent,
        "stage": stage,
        "message": message,
        "payload": payload,
        "ts": utcnow(),
    }


def _dossier(state: DeskState) -> dict:
    invoice = state.get("invoice") or {}
    return {
        "policy_decision": state.get("policy_decision"),
        "policy_rationale": state.get("policy_rationale"),
        "flags": state.get("flags") or [],
        "critique_notes": state.get("critique_notes") or "",
        "arithmetic": state.get("arithmetic") or {},
        "invoice": {
            "invoice_number": invoice.get("invoice_number"),
            "vendor_name": invoice.get("vendor_name"),
            "total": invoice.get("total"),
            "currency": invoice.get("currency"),
            "invoice_date": invoice.get("invoice_date"),
            "due_date": invoice.get("due_date"),
            "lines": [
                {
                    "name": line.get("raw_name"),
                    "sku": line.get("sku"),
                    "quantity": line.get("quantity"),
                    "unit_price": line.get("unit_price"),
                }
                for line in invoice.get("lines") or []
            ],
        },
    }
