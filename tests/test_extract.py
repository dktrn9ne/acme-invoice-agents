from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from northline.extract import extraction_diff, fix_ocr, parse_date, parse_document, vendor_compare_key

INVOICES = Path(__file__).resolve().parents[1] / "data" / "invoices"


def test_ocr_repairs_digits_and_leaves_invoice_words():
    raw = "DATE: 26-Jan-2O26  TOTAL 3,500.O0  INV-1001  WidgetA"
    fixed = fix_ocr(raw)
    assert "2026" in fixed
    assert "3,500.00" in fixed
    assert "INV-1001" in fixed
    assert "WidgetA" in fixed


def test_dates_cover_the_formats_in_the_corpus():
    assert parse_date("2026-01-30") == date(2026, 1, 30)
    assert parse_date("01/28/2026") == date(2026, 1, 28)
    assert parse_date("Jan 30 2026") == date(2026, 1, 30)
    assert parse_date("January 27, 2026") == date(2026, 1, 27)
    assert parse_date("26-Jan-2026") == date(2026, 1, 26)
    assert parse_date("yesterday") is None


def test_messy_1002_normalizes_the_typos():
    invoice = parse_document(INVOICES / "invoice_1002.txt")
    assert invoice.invoice_number == "INV-1002"
    assert invoice.vendor_name == "Gadgets Co."
    assert invoice.lines[0].sku == "GadgetX"
    assert invoice.lines[0].quantity == Decimal("20")
    assert invoice.total == Decimal("15000.00")
    assert invoice.due_date == invoice.invoice_date


def test_1008_email_body_and_1006_key_value_csv():
    email = parse_document(INVOICES / "invoice_1008.txt")
    assert email.vendor_name == "NoProd Industries"
    assert [line.raw_name for line in email.lines] == ["SuperGizmo", "MegaSprocket"]
    assert email.total == Decimal("9900.00")
    csv_invoice = parse_document(INVOICES / "invoice_1006.csv")
    assert csv_invoice.invoice_number == "INV-1006"
    assert [(line.sku, line.quantity) for line in csv_invoice.lines] == [
        ("WidgetA", Decimal("5")),
        ("WidgetB", Decimal("3")),
    ]


def test_us_dates_and_1012_ocr_skus():
    us = parse_document(INVOICES / "invoice_1007.csv")
    assert us.invoice_date == date(2026, 1, 28)
    assert us.due_date == date(2026, 2, 28)
    messy = parse_document(INVOICES / "invoice_1012.txt")
    assert messy.invoice_date == date(2026, 1, 26)
    assert [line.sku for line in messy.lines] == ["WidgetA", "WidgetB", "GadgetX"]
    assert messy.total == Decimal("9975.00")


def test_pdf_1013_keeps_vendor_and_due_date_on_shared_lines():
    invoice = parse_document(INVOICES / "invoice_1013.pdf")
    assert invoice.vendor_name == "Atlas Industrial Supply"
    assert invoice.invoice_date == date(2026, 1, 24)
    assert invoice.due_date == date(2026, 3, 24)
    assert len(invoice.lines) == 8
    assert invoice.total == Decimal("22562.80")


def test_extraction_diff_names_the_fields_that_moved():
    rules = parse_document(INVOICES / "invoice_1004.json")
    other = rules.model_copy(deep=True)
    other.vendor_name = "Someone Else"
    other.total = Decimal("1.00")
    diff = extraction_diff(rules, other)
    fields = {row["field"] for row in diff}
    assert fields == {"vendor_name", "total"}
    total = next(row for row in diff if row["field"] == "total")
    assert total["rules"] == "1890.00"
    assert total["grok"] == "1.00"
    assert extraction_diff(rules, rules) == []


def test_formerly_suffix_is_not_a_different_vendor():
    rules = parse_document(INVOICES / "invoice_1012.txt")
    assert rules.vendor_name == "QuickShip Distributers"
    other = rules.model_copy(deep=True)
    other.vendor_name = "QuickShip Distributers (formerly FastShip Ltd.)"
    assert vendor_compare_key(rules.vendor_name) == vendor_compare_key(other.vendor_name)
    assert extraction_diff(rules, other) == []
    other.vendor_name = "Someone Else (formerly FastShip Ltd.)"
    fields = {row["field"] for row in extraction_diff(rules, other)}
    assert "vendor_name" in fields


def test_xml_currency_and_json_revision():
    euro = parse_document(INVOICES / "invoice_1014.xml")
    assert euro.currency == "EUR"
    assert euro.total == Decimal("4125.00")
    revised = parse_document(INVOICES / "invoice_1004_revised.json")
    assert revised.revision == "R1"
    assert revised.invoice_number == "INV-1004"
    assert len(revised.lines) == 3
