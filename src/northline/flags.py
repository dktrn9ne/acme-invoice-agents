"""Flag catalog. Severity is policy: block never pays, review never auto-pays."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SEVERITY = {
    "stock_exceeded": "block",
    "zero_stock": "block",
    "unknown_item": "block",
    "negative_quantity": "block",
    "zero_quantity": "block",
    "arithmetic_mismatch": "block",
    "missing_vendor": "block",
    "missing_due_date": "block",
    "due_date_invalid": "block",
    "suspicious_address": "block",
    "duplicate_payment": "block",
    "price_variance": "review",
    "threshold_avoidance": "review",
    "foreign_currency": "review",
    "unapproved_vendor": "review",
    "duplicate_invoice": "review",
    "revision": "review",
    "vendor_renamed": "review",
    "urgency_language": "review",
    "wire_request": "review",
    "high_value": "info",
    "due_same_day": "info",
    "cross_format_duplicate": "info",
    "ocr_anomaly": "info",
    "shipping_charge": "info",
    "duplicate_sku": "info",
}

DECISION_RANK = {"paid": 0, "needs_review": 1, "rejected": 2}


@dataclass
class Flag:
    code: str
    severity: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "detail": self.detail,
        }


def make_flag(code: str, message: str, **detail: Any) -> Flag:
    if code not in SEVERITY:
        raise KeyError(f"Unknown flag code: {code}")
    return Flag(code=code, severity=SEVERITY[code], message=message, detail=detail)


def fold(flags: list[Flag]) -> list[Flag]:
    """One row per code. Repeated SKU findings are joined into a single explanation."""
    grouped: dict[str, list[Flag]] = {}
    order: list[str] = []
    for item in flags:
        if item.code not in grouped:
            order.append(item.code)
            grouped[item.code] = []
        grouped[item.code].append(item)
    folded: list[Flag] = []
    for code in order:
        group = grouped[code]
        if len(group) == 1:
            folded.append(group[0])
            continue
        folded.append(
            Flag(
                code=code,
                severity=group[0].severity,
                message="; ".join(item.message for item in group),
                detail={"items": [item.detail for item in group]},
            )
        )
    return folded


def tighter(proposed: str, floor: str) -> str:
    """A model may hold or reject more strictly than policy. It may not loosen it."""
    if proposed not in DECISION_RANK:
        return floor
    if DECISION_RANK[proposed] < DECISION_RANK[floor]:
        return floor
    return proposed
