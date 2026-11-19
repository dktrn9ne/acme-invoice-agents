"""Deterministic controls. This module is the only thing allowed to clear money."""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from northline.corpus import corpus_flags
from northline.extract import fix_ocr
from northline.flags import Flag, fold, make_flag
from northline.models import Invoice
from northline.money import D, TOLERANCE, q

HIGH_VALUE = Decimal("10000")
VARIANCE = Decimal("0.05")
THRESHOLD_FLOOR = Decimal("9500")

SUSPICIOUS = ("1600pennsylvania", "whitehouse")


class Ledger:
    """Tool surface the validation node calls. Every call is traced."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.trace: list[dict] = []
        self._sku_cache: dict[str, dict | None] = {}
        self._vendor_cache: dict[str, dict | None] = {}

    def lookup_sku(self, sku: str) -> dict | None:
        if sku in self._sku_cache:
            return self._sku_cache[sku]
        row = self.conn.execute(
            "SELECT sku, item_name, stock, unit_price, currency FROM inventory WHERE sku = ?",
            (sku,),
        ).fetchone()
        found = dict(row) if row else None
        self.trace.append({"tool": "lookup_sku", "sku": sku, "found": found is not None, "stock": None if not found else found["stock"]})
        self._sku_cache[sku] = found
        return found

    def lookup_vendor(self, name: str) -> dict | None:
        cleaned = " ".join(name.split())
        key = cleaned.lower()
        if key in self._vendor_cache:
            return self._vendor_cache[key]
        row = self.conn.execute(
            "SELECT name, status, currency, notes FROM vendors WHERE lower(name) = lower(?)",
            (cleaned,),
        ).fetchone()
        if row is None:
            alias = self.conn.execute(
                """
                SELECT v.name, v.status, v.currency, v.notes
                FROM vendor_aliases a
                JOIN vendors v ON v.name = a.vendor_name
                WHERE lower(a.alias) = lower(?)
                """,
                (cleaned,),
            ).fetchone()
            row = alias
        found = dict(row) if row else None
        self.trace.append(
            {
                "tool": "lookup_vendor",
                "vendor": cleaned,
                "status": None if not found else found["status"],
            }
        )
        self._vendor_cache[key] = found
        return found

    def find_payment(self, invoice_number: str) -> dict | None:
        row = self.conn.execute(
            """
            SELECT invoice_number, vendor, amount, currency, run_id, confirmation, created_at
            FROM payments
            WHERE invoice_number = ? AND status = 'success'
            ORDER BY id DESC
            LIMIT 1
            """,
            (invoice_number,),
        ).fetchone()
        found = dict(row) if row else None
        self.trace.append(
            {
                "tool": "find_payment",
                "invoice_number": invoice_number,
                "already_paid": found is not None,
            }
        )
        return found


def goods_subtotal(invoice: Invoice) -> Decimal:
    total = Decimal("0")
    for line in invoice.lines:
        if line.kind != "goods":
            continue
        total += q(line.quantity * line.unit_price)
    return q(total)


def validate_invoice(invoice: Invoice, ledger: Ledger, source: Path | None = None) -> list[Flag]:
    flags: list[Flag] = []
    flags.extend(_vendor_flags(invoice, ledger))
    flags.extend(_date_flags(invoice))
    flags.extend(_line_flags(invoice, ledger))
    flags.extend(_arithmetic_flags(invoice))
    flags.extend(_amount_flags(invoice))
    flags.extend(_text_flags(invoice))
    if invoice.invoice_number:
        prior = ledger.find_payment(invoice.invoice_number)
        if prior:
            flags.append(
                make_flag(
                    "duplicate_payment",
                    f"{invoice.invoice_number} was already paid ({prior['amount']} {prior['currency']}, confirmation {prior['confirmation']}).",
                    confirmation=prior["confirmation"],
                    amount=prior["amount"],
                )
            )
    path = source or (Path(invoice.source_path) if invoice.source_path else None)
    if path is not None:
        flags.extend(corpus_flags(path, invoice.invoice_number, invoice.revision))
    return fold(flags)


def _vendor_flags(invoice: Invoice, ledger: Ledger) -> list[Flag]:
    name = (invoice.vendor_name or "").strip()
    if not name:
        return [make_flag("missing_vendor", "Vendor name is blank, so there is nowhere to send the money.")]
    record = ledger.lookup_vendor(name)
    if record is None or record["status"] != "approved":
        return [
            make_flag(
                "unapproved_vendor",
                f"{name} is not on the approved vendor list.",
                vendor=name,
            )
        ]
    return []


def _date_flags(invoice: Invoice) -> list[Flag]:
    raw = (invoice.due_date_raw or "").strip()
    if invoice.due_date is None and not raw:
        return [make_flag("missing_due_date", "No due date was provided.")]
    if invoice.due_date is None:
        return [make_flag("due_date_invalid", f"Due date {raw!r} is not a calendar date.")]
    if invoice.invoice_date and invoice.due_date < invoice.invoice_date:
        return [
            make_flag(
                "due_date_invalid",
                f"Due date {invoice.due_date.isoformat()} is before the invoice date {invoice.invoice_date.isoformat()}.",
            )
        ]
    if invoice.invoice_date and invoice.due_date == invoice.invoice_date:
        return [
            make_flag(
                "due_same_day",
                f"Due date and invoice date are both {invoice.due_date.isoformat()}.",
            )
        ]
    return []


def _line_flags(invoice: Invoice, ledger: Ledger) -> list[Flag]:
    flags: list[Flag] = []
    quantities: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    appearances: dict[str, int] = defaultdict(int)
    varied: set[str] = set()
    currency = (invoice.currency or "USD").upper()
    for line in invoice.lines:
        if line.kind == "charge":
            amount = line.line_total or q(line.quantity * line.unit_price)
            flags.append(
                make_flag(
                    "shipping_charge",
                    f"A {line.raw_name.lower()} charge of {amount} was kept out of the stock check and included in the total.",
                    amount=str(amount),
                )
            )
            continue
        if line.quantity < 0:
            flags.append(
                make_flag(
                    "negative_quantity",
                    f"{line.raw_name} has quantity {line.quantity}.",
                    sku=line.raw_name,
                    quantity=str(line.quantity),
                )
            )
            continue
        if line.quantity == 0:
            flags.append(
                make_flag(
                    "zero_quantity",
                    f"{line.raw_name} has quantity 0.",
                    sku=line.raw_name,
                )
            )
            continue
        if not line.sku:
            flags.append(
                make_flag(
                    "unknown_item",
                    f"{line.raw_name} is not in the item master.",
                    name=line.raw_name,
                )
            )
            continue
        record = ledger.lookup_sku(line.sku)
        if record is None:
            flags.append(
                make_flag(
                    "unknown_item",
                    f"{line.sku} is not in the item master.",
                    name=line.sku,
                )
            )
            continue
        quantities[line.sku] += line.quantity
        appearances[line.sku] += 1
        catalog = D(record["unit_price"]) if record["unit_price"] is not None else None
        if currency == "USD" and catalog is not None and catalog > 0 and line.sku not in varied:
            gap = abs(line.unit_price - catalog) / catalog
            if gap > VARIANCE:
                varied.add(line.sku)
                pct = (gap * Decimal("100")).quantize(Decimal("0.1"))
                flags.append(
                    make_flag(
                        "price_variance",
                        f"{line.sku} is billed at {line.unit_price} against a catalog price of {catalog} ({pct}% off catalog).",
                        sku=line.sku,
                        billed=str(line.unit_price),
                        catalog=str(catalog),
                        percent=str(pct),
                    )
                )
    for sku, count in appearances.items():
        if count > 1:
            flags.append(
                make_flag(
                    "duplicate_sku",
                    f"{sku} appears on {count} lines (aggregated quantity {quantities[sku]}).",
                    sku=sku,
                    lines=count,
                    quantity=str(quantities[sku]),
                )
            )
    for sku, quantity in quantities.items():
        record = ledger.lookup_sku(sku)
        if record is None:
            continue
        stock = Decimal(record["stock"])
        if stock <= 0:
            flags.append(
                make_flag(
                    "zero_stock",
                    f"{sku} is in the catalog with {record['stock']} on hand, and the invoice asks for {quantity}.",
                    sku=sku,
                    stock=record["stock"],
                    quantity=str(quantity),
                )
            )
        elif quantity > stock:
            flags.append(
                make_flag(
                    "stock_exceeded",
                    f"{sku} aggregates to {quantity}, above {record['stock']} on hand.",
                    sku=sku,
                    stock=record["stock"],
                    quantity=str(quantity),
                )
            )
    return flags


def _arithmetic_flags(invoice: Invoice) -> list[Flag]:
    computed = goods_subtotal(invoice)
    problems: list[str] = []
    if invoice.subtotal is not None and abs(invoice.subtotal - computed) > TOLERANCE:
        problems.append(f"stated subtotal {invoice.subtotal} vs lines {computed}")
    basis = invoice.subtotal if invoice.subtotal is not None else computed
    if invoice.tax_rate is not None and invoice.tax_amount is not None:
        expected_tax = q(basis * invoice.tax_rate)
        if abs(expected_tax - invoice.tax_amount) > TOLERANCE:
            problems.append(
                f"stated tax {invoice.tax_amount} vs {invoice.tax_rate} of {basis} = {expected_tax}"
            )
    for line in invoice.lines:
        if line.kind != "goods" or line.line_total is None:
            continue
        expected = q(line.quantity * line.unit_price)
        if abs(expected - line.line_total) > TOLERANCE:
            problems.append(f"{line.raw_name} line total {line.line_total} vs {expected}")
    if invoice.total is not None:
        tax = invoice.tax_amount or Decimal("0")
        shipping = invoice.shipping or Decimal("0")
        expected_total = q(basis + tax + shipping)
        if abs(expected_total - invoice.total) > TOLERANCE:
            problems.append(f"stated total {invoice.total} vs subtotal + tax + shipping {expected_total}")
    if not problems:
        return []
    return [
        make_flag(
            "arithmetic_mismatch",
            "The printed totals do not match the lines: " + "; ".join(problems) + ".",
            problems=problems,
        )
    ]


def _amount_flags(invoice: Invoice) -> list[Flag]:
    flags: list[Flag] = []
    amounts = _candidate_amounts(invoice)
    if any(amount > HIGH_VALUE for amount in amounts):
        shown = invoice.total if invoice.total is not None else amounts[0]
        flags.append(
            make_flag(
                "high_value",
                f"Total {shown} is above the $10,000 line that requires VP scrutiny.",
                total=str(shown),
            )
        )
    elif any(THRESHOLD_FLOOR <= amount < HIGH_VALUE for amount in amounts):
        shown = invoice.total if invoice.total is not None else amounts[0]
        flags.append(
            make_flag(
                "threshold_avoidance",
                f"Total {shown} sits just under the $10,000 control (within $500).",
                total=str(shown),
            )
        )
    if invoice.shipping is not None and invoice.shipping != 0:
        flags.append(
            make_flag(
                "shipping_charge",
                f"A shipping charge of {invoice.shipping} was kept out of the stock check and included in the total.",
                amount=str(invoice.shipping),
            )
        )
    if (invoice.currency or "USD").upper() != "USD":
        flags.append(
            make_flag(
                "foreign_currency",
                f"Currency is {invoice.currency}. This prototype pays USD only, so the invoice is held for FX.",
                currency=invoice.currency,
            )
        )
    address = invoice.vendor_address or ""
    squashed = re.sub(r"[^a-z0-9]", "", address.lower())
    if any(needle in squashed for needle in SUSPICIOUS):
        flags.append(
            make_flag(
                "suspicious_address",
                f"Vendor address matches a known non-operating address: {address}.",
                address=address,
            )
        )
    return flags


def _candidate_amounts(invoice: Invoice) -> list[Decimal]:
    amounts: list[Decimal] = []
    if invoice.total is not None:
        amounts.append(invoice.total)
    computed = q(goods_subtotal(invoice) + (invoice.tax_amount or Decimal("0")) + (invoice.shipping or Decimal("0")))
    if invoice.lines:
        amounts.append(computed)
    return amounts


def _text_flags(invoice: Invoice) -> list[Flag]:
    raw = invoice.raw_text or ""
    flags: list[Flag] = []
    if re.search(r"\burgent\b|\bimmediately\b|avoid penalties|\basap\b", raw, re.IGNORECASE):
        flags.append(
            make_flag(
                "urgency_language",
                "The document presses for immediate payment or threatens penalties.",
            )
        )
    if re.search(r"wire transfer|\bwire\b", raw, re.IGNORECASE):
        flags.append(make_flag("wire_request", "The document asks to be paid by wire."))
    if re.search(r"\bformerly\b", raw, re.IGNORECASE):
        flags.append(
            make_flag(
                "vendor_renamed",
                "The document says the vendor formerly operated under another name.",
            )
        )
    if _ocr_anomaly(raw):
        flags.append(
            make_flag(
                "ocr_anomaly",
                "The source has OCR artifacts (a letter standing in for a digit, or a split SKU). Values were normalized before the controls ran.",
            )
        )
    return flags


def _ocr_anomaly(raw: str) -> bool:
    if re.search(r"\bWidget\s+A\b|\bGadget\s+X\b", raw):
        return True
    return fix_ocr(raw) != raw
