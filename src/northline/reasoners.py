"""Reasoners behind one interface.

Offline templates keep `make eval` deterministic. Grok is used for messy
reads and for the approval narrative. Money gates stay in policy.py either way.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Protocol

import httpx

from northline.extract import draft_to_invoice, drafts_agree
from northline.flags import DECISION_RANK
from northline.models import Critique, Invoice, InvoiceDraft, Narrative, schema_errors

log = logging.getLogger("northline")

EXTRACT_SYSTEM = """You extract invoices. Copy printed values. Do not fix arithmetic, do not invent line items, and do not drop a line because it looks fraudulent.
Normalize obvious OCR: letter O used as zero, and SKUs that are split with a space (Widget A, Gadget X).
Return invoice_number like INV-1001. Dates as ISO YYYY-MM-DD when you can read them. Leave unreadable due dates as printed.
currency is a 3-letter code, USD if the document does not say otherwise.
tax_rate is a fraction (0.08 for 8 percent), copied only when a rate is printed.
quantity and unit_price are plain numbers. Shipping is a separate field, not a line item."""

NARRATE_SYSTEM = """You are the VP's desk reviewing a packet that deterministic controls already scored.
You may keep the policy decision, or you may be stricter (hold or reject). You may not loosen a block into a payment.
Mention the $10,000 scrutiny line whenever the packet is flagged high_value.
Write a short rationale a controller can read. decision must be one of paid, rejected, needs_review."""

CRITIQUE_SYSTEM = """You are the critic sitting next to the VP's desk.
If the draft decision is looser than the policy decision, verdict is revise.
If the invoice is high_value and the rationale never mentions the $10,000 line, verdict is revise.
Otherwise verdict is accept.
recommended_decision is the decision the desk should execute. Do not recommend a looser decision than policy."""


class Reasoner(Protocol):
    name: str

    def extract(self, raw_text: str, source_format: str, feedback: list[str], rules: Invoice) -> Invoice: ...

    def narrate(self, dossier: dict) -> Narrative: ...

    def critique(self, dossier: dict, narrative: Narrative) -> Critique: ...


class OfflineReasoner:
    """Rules read plus a templated VP note. Same graph, no network."""

    name = "offline"

    def extract(self, raw_text: str, source_format: str, feedback: list[str], rules: Invoice) -> Invoice:
        if feedback:
            rules.extraction_notes.append("Re-read after schema feedback: " + "; ".join(feedback))
        else:
            rules.extraction_notes.append("Offline reader. No model call.")
        return rules

    def narrate(self, dossier: dict) -> Narrative:
        decision = dossier["policy_decision"]
        codes = [item["code"] for item in dossier.get("flags") or []]
        parts = [f"Policy recommends {decision}."]
        if "high_value" in codes:
            parts.append("Amount exceeds the $10,000 VP scrutiny threshold.")
        if codes:
            parts.append("Flags: " + ", ".join(codes) + ".")
        else:
            parts.append("No control flags.")
        parts.append(dossier.get("policy_rationale") or "")
        notes = dossier.get("critique_notes") or ""
        if notes:
            parts.append(f"Revised after critique: {notes}")
        return Narrative(decision=decision, rationale=" ".join(part for part in parts if part), concerns=codes)

    def critique(self, dossier: dict, narrative: Narrative) -> Critique:
        policy = dossier["policy_decision"]
        if DECISION_RANK[narrative.decision] < DECISION_RANK[policy]:
            return Critique(
                verdict="revise",
                notes="Draft is looser than the controls allow.",
                recommended_decision=policy,
            )
        high = any(item["code"] == "high_value" for item in dossier.get("flags") or [])
        if high and "10,000" not in narrative.rationale and "10000" not in narrative.rationale:
            return Critique(
                verdict="revise",
                notes="High-value invoice and the draft never mentions the $10,000 threshold.",
                recommended_decision=narrative.decision,
            )
        return Critique(
            verdict="accept",
            notes="Draft matches the controls.",
            recommended_decision=narrative.decision,
        )


class ScriptedReasoner:
    """Test double. Each call pops the next scripted response."""

    name = "scripted"

    def __init__(self, extracts: list, narratives: list, critiques: list):
        self._extracts = list(extracts)
        self._narratives = list(narratives)
        self._critiques = list(critiques)
        self.extract_calls = 0
        self.narrate_calls = 0
        self.critique_calls = 0

    def extract(self, raw_text: str, source_format: str, feedback: list[str], rules: Invoice) -> Invoice:
        self.extract_calls += 1
        nxt = self._extracts.pop(0)
        return nxt(feedback, rules) if callable(nxt) else nxt

    def narrate(self, dossier: dict) -> Narrative:
        self.narrate_calls += 1
        nxt = self._narratives.pop(0)
        return nxt(dossier) if callable(nxt) else nxt

    def critique(self, dossier: dict, narrative: Narrative) -> Critique:
        self.critique_calls += 1
        nxt = self._critiques.pop(0)
        return nxt(dossier, narrative) if callable(nxt) else nxt


class GrokReasoner:
    """xAI Grok via LangChain, with a direct HTTPS fallback and a rules fallback."""

    name = "grok"

    def __init__(self, model: str, api_key: str, timeout: float = 45.0):
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self._offline = OfflineReasoner()

    def extract(self, raw_text: str, source_format: str, feedback: list[str], rules: Invoice) -> Invoice:
        if source_format in {"json", "csv", "xml"}:
            rules.extraction_notes.append("Structured file. Rules reader is authoritative.")
            return rules
        try:
            draft = self._structured(
                EXTRACT_SYSTEM,
                _extract_user(raw_text, feedback),
                InvoiceDraft,
            )
            parsed = draft_to_invoice(draft, raw_text, rules.source_path, rules.source_format)
        except Exception as exc:
            log.warning(json.dumps({"agent": "ingestion", "message": "grok_extract_failed", "error": exc.__class__.__name__}))
            rules.extraction_notes.append(f"Grok extraction failed ({exc.__class__.__name__}). Kept the rules reader.")
            return rules
        if schema_errors(parsed):
            parsed.extraction_notes.append("Grok draft failed schema checks.")
            return parsed
        if drafts_agree(parsed, rules):
            rules.extraction_notes.append("Grok extraction agrees with the rules reader.")
            return rules
        rules.extraction_notes.append("Grok disagreed with the rules reader. Money path kept the rules read.")
        return rules

    def narrate(self, dossier: dict) -> Narrative:
        try:
            narrative = self._structured(NARRATE_SYSTEM, json.dumps(dossier, default=str), Narrative)
        except Exception as exc:
            log.warning(json.dumps({"agent": "approval", "message": "grok_narrate_failed", "error": exc.__class__.__name__}))
            narrative = self._offline.narrate(dossier)
            narrative.rationale = f"[offline fallback after {exc.__class__.__name__}] " + narrative.rationale
            return narrative
        if narrative.decision not in DECISION_RANK:
            return self._offline.narrate(dossier)
        return narrative

    def critique(self, dossier: dict, narrative: Narrative) -> Critique:
        packet = {"dossier": dossier, "draft": narrative.model_dump()}
        try:
            critique = self._structured(CRITIQUE_SYSTEM, json.dumps(packet, default=str), Critique)
        except Exception as exc:
            log.warning(json.dumps({"agent": "critic", "message": "grok_critique_failed", "error": exc.__class__.__name__}))
            critique = self._offline.critique(dossier, narrative)
            critique.notes = f"[offline fallback after {exc.__class__.__name__}] " + critique.notes
            return critique
        if critique.verdict not in {"accept", "revise"}:
            return self._offline.critique(dossier, narrative)
        if critique.recommended_decision not in DECISION_RANK:
            critique.recommended_decision = narrative.decision
        return critique

    def _structured(self, system: str, user: str, schema):
        try:
            return self._via_langchain(system, user, schema)
        except Exception as first:
            log.info(json.dumps({"agent": "grok", "message": "langchain_fallback", "error": first.__class__.__name__}))
            return self._via_httpx(system, user, schema)

    def _via_langchain(self, system: str, user: str, schema):
        from langchain_xai import ChatXAI

        llm = ChatXAI(
            model=self.model,
            xai_api_key=self.api_key,
            request_timeout=self.timeout,
            max_retries=2,
        )
        structured = llm.with_structured_output(schema)
        result = structured.invoke(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
        )
        if isinstance(result, schema):
            return result
        return schema.model_validate(result)

    def _via_httpx(self, system: str, user: str, schema):
        schema_text = json.dumps(schema.model_json_schema())
        body_user = user + "\n\nRespond with one JSON object matching this schema and nothing else:\n" + schema_text
        last: Exception | None = None
        for attempt in range(3):
            try:
                response = httpx.post(
                    "https://api.x.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": body_user},
                        ],
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                return schema.model_validate(json.loads(_extract_json(content)))
            except Exception as exc:
                last = exc
                time.sleep(1.2 * (attempt + 1))
        raise RuntimeError(f"Grok request failed: {last.__class__.__name__}") from last


def _extract_user(raw_text: str, feedback: list[str]) -> str:
    parts = ["Document follows.", raw_text]
    if feedback:
        parts.append("Your previous draft failed these checks. Fix only these and keep every printed amount:")
        parts.extend(f"- {item}" for item in feedback)
    return "\n".join(parts)


def _extract_json(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[: text.rfind("```")]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text
