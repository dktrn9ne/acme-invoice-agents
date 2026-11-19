"""Turn flags into a payable decision. No model is consulted here."""

from __future__ import annotations

from northline.flags import Flag


def decide(flags: list[Flag] | list[dict]) -> str:
    severities = set()
    for item in flags:
        if isinstance(item, Flag):
            severities.add(item.severity)
        else:
            severities.add(item["severity"])
    if "block" in severities:
        return "rejected"
    if "review" in severities:
        return "needs_review"
    return "paid"


def explain(decision: str, flags: list[dict]) -> str:
    blocks = [item["message"] for item in flags if item["severity"] == "block"]
    reviews = [item["message"] for item in flags if item["severity"] == "review"]
    if decision == "rejected":
        return "Do not pay. " + " ".join(blocks)
    if decision == "needs_review":
        return "Hold for a person. " + " ".join(reviews)
    if any(item["code"] == "high_value" for item in flags):
        return (
            "Controls cleared, including the extra pass required above $10,000. "
            "Pay the stated total."
        )
    return "Controls cleared. Pay the stated total."
