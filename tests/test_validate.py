from __future__ import annotations

from decimal import Decimal

from pathlib import Path

from northline.extract import parse_document
from northline.policy import decide
from northline.validate import Ledger, validate_invoice

INVOICES = Path(__file__).resolve().parents[1] / "data" / "invoices"


def _flags(conn, name: str):
    path = INVOICES / name
    return validate_invoice(parse_document(path), Ledger(conn), path)


def _codes(conn, name: str) -> list[str]:
    return sorted(flag.code for flag in _flags(conn, name))


def test_1004_tax_is_exact_and_1007_total_is_not(conn):
    assert _codes(conn, "invoice_1004.json") == []
    flags = _flags(conn, "invoice_1007.csv")
    arithmetic = next(flag for flag in flags if flag.code == "arithmetic_mismatch")
    assert "15525.00" in arithmetic.message
    assert "15635.00" in arithmetic.message


def test_1013_aggregates_before_the_stock_check(conn):
    flags = _flags(conn, "invoice_1013.json")
    stock = next(flag for flag in flags if flag.code == "stock_exceeded")
    assert "WidgetA aggregates to 22" in stock.message
    assert "WidgetB aggregates to 18" in stock.message
    assert "GadgetX aggregates to 9" in stock.message
    assert decide(flags) == "rejected"
    assert "price_variance" not in {flag.code for flag in flags}


def test_1010_rush_price_is_a_hold_not_a_stock_failure(conn):
    flags = _flags(conn, "invoice_1010.txt")
    codes = {flag.code for flag in flags}
    assert "price_variance" in codes
    assert "duplicate_sku" in codes
    assert "shipping_charge" in codes
    assert "stock_exceeded" not in codes
    assert decide(flags) == "needs_review"


def test_1003_is_zero_stock_and_a_wire_rush(conn):
    codes = set(_codes(conn, "invoice_1003.txt"))
    assert {"zero_stock", "wire_request", "urgency_language", "due_date_invalid", "unapproved_vendor"} <= codes
    assert "stock_exceeded" not in codes


def test_1009_rejects_a_blank_vendor_and_a_negative_line(conn):
    codes = set(_codes(conn, "invoice_1009.json"))
    assert {"missing_vendor", "negative_quantity", "missing_due_date", "arithmetic_mismatch"} <= codes


def test_threshold_and_currency(conn):
    assert "threshold_avoidance" in _codes(conn, "invoice_1008.txt")
    assert "threshold_avoidance" in _codes(conn, "invoice_1012.txt")
    assert _codes(conn, "invoice_1014.xml") == ["foreign_currency"]
    assert "suspicious_address" in _codes(conn, "invoice_1005.json")
