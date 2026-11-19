"""Decimal money helpers. Floats never cross a decision boundary."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENT = Decimal("0.01")
TOLERANCE = Decimal("0.02")


def D(value: object) -> Decimal | None:
    """Parse a money-ish value. Returns None for blanks."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value.quantize(CENT, rounding=ROUND_HALF_UP)
    text = str(value).strip()
    if text == "" or text.lower() in {"null", "none", "n/a", "na"}:
        return None
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    text = text.replace("$", "").replace(",", "").replace(" ", "")
    if text.startswith("-"):
        negative = True
        text = text[1:]
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if negative:
        number = -number
    return number.quantize(CENT, rounding=ROUND_HALF_UP)


def q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def money_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return f"{q(value):.2f}"


def close(a: Decimal | None, b: Decimal | None, tol: Decimal = TOLERANCE) -> bool:
    if a is None or b is None:
        return False
    return abs(q(a) - q(b)) <= tol
