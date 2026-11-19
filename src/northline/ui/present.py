"""Sentences for the desk. The audit log keeps the original message."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from northline.flags import SEVERITY, flag_label

_ARITHMETIC = re.compile(
    r"Arithmetic(?: \(authoritative\))?: line sum ([0-9.]+), tax ([0-9.]+), "
    r"expected total ([0-9.]+), stated total ([0-9.]+), total_matches (True|False)\."
)
_FLAGS = re.compile(r"Flags: ([a-z0-9_, ]+)\.")

_TOOL = {
    "lookup_vendor": "the vendor list",
    "lookup_sku": "the item master",
    "find_payment": "whether this invoice was already paid",
}
_RECOMMENDATION = {
    "paid": "paying this invoice",
    "rejected": "blocking payment",
    "needs_review": "holding this for a person",
}


def format_local(value: str | None) -> str:
    """UTC timestamp from the log, shown in the machine's local zone."""
    if not value:
        return "—"
    text = value.replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone()
    zone = local.tzname() or "local"
    return local.strftime("%Y-%m-%d %H:%M ") + zone


def humanize_event(event: dict) -> str:
    message = event.get("message") or ""
    payload = event.get("payload") or {}
    if event.get("stage") == "validate" and payload.get("arithmetic"):
        return _validation_sentence(message, payload)
    return _prettify(message)


def _validation_sentence(message: str, payload: dict) -> str:
    found = re.search(r"Controls recommend (\w+)", message)
    decision = found.group(1) if found else ""
    verb = _RECOMMENDATION.get(decision, decision or "a decision")
    seen: list[str] = []
    for tool in payload.get("tools") or []:
        label = _TOOL.get(tool.get("tool"), str(tool.get("tool") or "").replace("_", " "))
        if label and label not in seen:
            seen.append(label)
    checked = ""
    if seen:
        checked = " Checked " + _join(seen) + "."
    return f"The controls recommend {verb}.{checked} {_arithmetic_sentence(payload['arithmetic'])}"


def _money(value: object) -> str:
    from northline.ui.app import format_money

    return format_money(value)


def _arithmetic_sentence(facts: dict) -> str:
    line = _money(facts.get("line_sum"))
    tax = _money(facts.get("stated_tax") or "0")
    stated = _money(facts.get("stated_total"))
    expected = _money(facts.get("expected_total"))
    if facts.get("total_matches"):
        return f"Lines sum to {line}, tax is {tax}, and the stated total of {stated} matches."
    return (
        f"Lines sum to {line} and tax is {tax}. "
        f"The stated total of {stated} does not match the expected {expected}."
    )


def _prettify(message: str) -> str:
    text = _ARITHMETIC.sub(lambda match: _arithmetic_from_text(match) + " ", message)

    def flags(match: re.Match[str]) -> str:
        codes = [part.strip() for part in match.group(1).split(",") if part.strip()]
        return "Flags: " + ", ".join(flag_label(code) for code in codes) + "."

    text = _FLAGS.sub(flags, text)
    for code in sorted(SEVERITY, key=len, reverse=True):
        text = text.replace(code, flag_label(code))
    return " ".join(text.split())


def _arithmetic_from_text(match: re.Match[str]) -> str:
    line, tax, expected, stated, matches = match.groups()
    facts = {
        "line_sum": line,
        "stated_tax": tax,
        "expected_total": expected,
        "stated_total": stated,
        "total_matches": matches == "True",
    }
    return _arithmetic_sentence(facts).rstrip(".")


def _join(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + ", and " + items[-1]
