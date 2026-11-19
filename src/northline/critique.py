"""Critic rules that do not depend on the model.

A draft may be stricter than policy. It may not be looser, and it may not
invent an arithmetic problem the controls already settled.
"""

from __future__ import annotations

import re

from northline.flags import DECISION_RANK
from northline.models import Critique, Narrative

# Claims that the printed money is wrong. Used only when the controls say it is right,
# or the inverse when the controls say it is wrong.
_MISMATCH = re.compile(
    r"does not match|doesn't match|do not match|don't match|"
    r"does not add up|doesn't add up|do not add up|"
    r"\bmismatch\b|inconsistent total|totals? (?:is|are) wrong|"
    r"forgot(?:ten)? (?:the )?tax|ignor(?:es|ed|ing) (?:the )?tax|without (?:the )?tax|"
    r"not equal to the (?:lines|line items)|discrepan",
    re.IGNORECASE,
)
_CLEAN = re.compile(
    r"totals? match|arithmetic (?:is|checks) (?:ok|correct|clean)|no arithmetic|"
    r"sums? (?:is|are) correct",
    re.IGNORECASE,
)


def rationale_contradicts(rationale: str, arithmetic: dict | None) -> str | None:
    """Return an explanation when the draft fights the computed arithmetic."""
    if not arithmetic or not rationale:
        return None
    text = rationale
    clean = bool(
        arithmetic.get("total_matches")
        and arithmetic.get("subtotal_matches_lines")
        and arithmetic.get("tax_matches")
    )
    if clean and _MISMATCH.search(text):
        return (
            "Draft claims the money does not add up. "
            f"Controls: line sum {arithmetic.get('line_sum')}, "
            f"tax {arithmetic.get('stated_tax') or '0.00'} "
            f"(rate {arithmetic.get('tax_rate') or 'n/a'}), "
            f"expected total {arithmetic.get('expected_total')}, "
            f"stated total {arithmetic.get('stated_total')}. "
            "total_matches is true. Use these figures. Do not recompute."
        )
    broken = not arithmetic.get("total_matches") or not arithmetic.get("tax_matches") or not arithmetic.get(
        "subtotal_matches_lines"
    )
    if broken and _CLEAN.search(text):
        return (
            "Draft says the arithmetic is clean. "
            f"Controls: total_matches {arithmetic.get('total_matches')}, "
            f"tax_matches {arithmetic.get('tax_matches')}, "
            f"subtotal_matches_lines {arithmetic.get('subtotal_matches_lines')}, "
            f"expected total {arithmetic.get('expected_total')}, "
            f"stated total {arithmetic.get('stated_total')}."
        )
    return None


def enforce_critique(dossier: dict, narrative: Narrative, model: Critique) -> Critique:
    """Apply the non-negotiable critic rules on top of whatever the model said."""
    policy = dossier.get("policy_decision") or narrative.decision
    if narrative.decision in DECISION_RANK and policy in DECISION_RANK:
        if DECISION_RANK[narrative.decision] < DECISION_RANK[policy]:
            return Critique(
                verdict="revise",
                notes="Draft is looser than the controls allow.",
                recommended_decision=policy,
            )
    contradiction = rationale_contradicts(narrative.rationale, dossier.get("arithmetic"))
    if contradiction:
        prefix = ""
        if model.verdict == "accept":
            prefix = "Model critic accepted a draft that contradicts the computed arithmetic. "
        return Critique(
            verdict="revise",
            notes=prefix + contradiction,
            recommended_decision=policy if policy in DECISION_RANK else narrative.decision,
        )
    high = any(item.get("code") == "high_value" for item in dossier.get("flags") or [])
    if high and "10,000" not in narrative.rationale and "10000" not in narrative.rationale:
        return Critique(
            verdict="revise",
            notes="High-value invoice and the draft never mentions the $10,000 threshold.",
            recommended_decision=narrative.decision if narrative.decision in DECISION_RANK else policy,
        )
    if model.verdict not in {"accept", "revise"}:
        return Critique(verdict="accept", notes="Draft matches the controls.", recommended_decision=narrative.decision)
    if model.recommended_decision not in DECISION_RANK:
        model.recommended_decision = narrative.decision
    return model
