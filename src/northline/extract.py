"""Format-aware loaders and a deterministic reader for messy invoices.

The live path may ask Grok to read text and PDFs. This module is the control:
structured files never go through a model, and a model draft that disagrees
with a complete rules read is recorded and then set aside.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from northline.models import DraftLine, Invoice, InvoiceDraft, LineItem
from northline.money import D

KNOWN_SKUS = {
    "widgeta": "WidgetA",
    "widgetb": "WidgetB",
    "widgetc": "WidgetC",
    "gadgetx": "GadgetX",
    "fakeitem": "FakeItem",
}

MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

ITEM_QTY = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z0-9&.'()/ +-]*?)\s+"
    r"qty:?\s*(?P<qty>-?\d+)\s+"
    r"(?:@\s*)?(?:unit\s*price:?\s*)?"
    r"\$?(?P<price>\d[\d,]*(?:\.\d+)?)",
    re.IGNORECASE,
)
ITEM_X = re.compile(
    r"^-?\s*(?P<name>[A-Za-z][A-Za-z0-9&.'()/ +-]*?)\s+"
    r"x\s*(?P<qty>-?\d+)\s+"
    r"\$?(?P<price>\d[\d,]*(?:\.\d+)?)",
    re.IGNORECASE,
)
ITEM_TABLE = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z0-9&.'()/ +-]*?)\s+"
    r"(?P<qty>-?\d+)\s+"
    r"\$?(?P<price>\d[\d,]*(?:\.\d+)?)\s+"
    r"\$?(?P<amount>\d[\d,]*\.\d{2})"
    r"(?:\s+(?P<note>.*))?$"
)
SUB_RE = re.compile(r"^subtotal\s*:\s*\$?(?P<amt>-?\d[\d,]*\.\d{2})", re.IGNORECASE)
TAX_RE = re.compile(
    r"^(?:sales\s+tax|tax)\s*(?:\((?P<rate>\d+(?:\.\d+)?)\s*%\))?\s*:\s*"
    r"\$?(?P<amt>-?\d[\d,]*\.\d{2})",
    re.IGNORECASE,
)
SHIP_RE = re.compile(r"^shipping\s*:\s*\$?(?P<amt>\d[\d,]*\.\d{2})", re.IGNORECASE)
TOTAL_RE = re.compile(
    r"^(?:grand\s+total|total\s+amount|total|amt)\s*:\s*\$?(?P<amt>-?\d[\d,]*\.\d{2})",
    re.IGNORECASE,
)
NUMBER_ANYWHERE = re.compile(r"INV[-\s]?(\d{4})", re.IGNORECASE)


def fix_ocr(text: str) -> str:
    """Swap OCR lookalikes inside tokens that are otherwise numeric.

    `INV-1001` is left alone. `2O26` and `3,500.O0` are not.
    """

    def repl(match: re.Match[str]) -> str:
        token = match.group(0)
        core = re.sub(r"[$,.]", "", token)
        if not core:
            return token
        digits = sum(character.isdigit() for character in core)
        confusing = sum(character in "OoIl" for character in core)
        letters = sum(character.isalpha() for character in core)
        if digits >= 1 and letters == confusing and confusing >= 1:
            return (
                token.replace("O", "0")
                .replace("o", "0")
                .replace("I", "1")
                .replace("l", "1")
            )
        return token

    return re.sub(r"[A-Za-z0-9$][A-Za-z0-9$,.]*", repl, text)


def normalize_invoice_number(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"(\d{4})", str(value))
    if not match:
        return None
    return f"INV-{match.group(1)}"


def parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    text = " ".join(str(value).replace(",", " ").split())
    if not text or text.lower() in {"yesterday", "today", "tomorrow", "asap", "immediate", "immediately"}:
        return None
    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if iso:
        return _ymd(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    us = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if us:
        return _ymd(int(us.group(3)), int(us.group(1)), int(us.group(2)))
    dmy = re.fullmatch(r"(\d{1,2})-([A-Za-z]+)-(\d{4})", text)
    if dmy:
        month = MONTHS.get(dmy.group(2).lower())
        if month:
            return _ymd(int(dmy.group(3)), month, int(dmy.group(1)))
    mdy = re.fullmatch(r"([A-Za-z]+) (\d{1,2}) (\d{4})", text)
    if mdy:
        month = MONTHS.get(mdy.group(1).lower())
        if month:
            return _ymd(int(mdy.group(3)), month, int(mdy.group(2)))
    return None


def _ymd(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def canonical_sku(name: str) -> tuple[str | None, str | None]:
    note_match = re.search(r"\(([^)]+)\)", name)
    note = note_match.group(1).strip() if note_match else None
    base = re.sub(r"\([^)]*\)", " ", name)
    key = re.sub(r"[^a-z0-9]", "", base.lower())
    return KNOWN_SKUS.get(key), note


def make_line(
    name: str,
    quantity: Decimal | None,
    unit_price: Decimal | None,
    line_total: Decimal | None = None,
    note: str | None = None,
) -> LineItem | None:
    clean = " ".join(name.split()).strip(" -")
    if not clean or quantity is None or unit_price is None:
        return None
    if re.match(r"^(shipping|freight|delivery|handling)\b", clean, re.IGNORECASE):
        return LineItem(
            raw_name=clean,
            sku=None,
            quantity=quantity,
            unit_price=unit_price,
            line_total=line_total,
            note=note,
            kind="charge",
        )
    sku, paren = canonical_sku(clean)
    return LineItem(
        raw_name=clean,
        sku=sku,
        quantity=quantity,
        unit_price=unit_price,
        line_total=line_total,
        note=note or paren,
        kind="goods",
    )


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def read_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def parse_document(path: Path, raw_text: str | None = None) -> Invoice:
    path = Path(path)
    suffix = path.suffix.lower()
    if raw_text is None:
        raw_text = read_pdf(path) if suffix == ".pdf" else read_text(path)
    if suffix == ".json":
        invoice = parse_json(raw_text)
    elif suffix == ".csv":
        invoice = parse_csv(raw_text)
    elif suffix == ".xml":
        invoice = parse_xml(raw_text)
    else:
        invoice = parse_text(raw_text)
    invoice.raw_text = raw_text
    invoice.source_path = str(path)
    invoice.source_format = suffix.lstrip(".") or "txt"
    if not invoice.invoice_number:
        invoice.invoice_number = _number_from_text(raw_text)
    return invoice


def parse_json(raw: str) -> Invoice:
    data = json.loads(raw, parse_float=Decimal)
    vendor = data.get("vendor")
    address = None
    if isinstance(vendor, dict):
        name = vendor.get("name") or ""
        address = vendor.get("address")
    else:
        name = vendor or ""
    lines = []
    for row in data.get("line_items") or []:
        line = make_line(
            str(row.get("item") or row.get("name") or ""),
            D(row.get("quantity")),
            D(row.get("unit_price")),
            D(row.get("amount")),
            row.get("note"),
        )
        if line:
            lines.append(line)
    due_raw = data.get("due_date")
    date_raw = data.get("date")
    return Invoice(
        invoice_number=normalize_invoice_number(data.get("invoice_number")),
        vendor_name=(name or "").strip(),
        vendor_address=address if address else None,
        invoice_date=parse_date(None if date_raw is None else str(date_raw)),
        due_date=parse_date(None if due_raw is None else str(due_raw)),
        invoice_date_raw=None if date_raw is None else str(date_raw),
        due_date_raw=None if due_raw is None else str(due_raw),
        currency=(data.get("currency") or "USD").upper(),
        lines=lines,
        subtotal=D(data.get("subtotal")),
        tax_rate=_rate(data.get("tax_rate")),
        tax_amount=D(data.get("tax_amount") if "tax_amount" in data else data.get("tax")),
        shipping=D(data.get("shipping")),
        total=D(data.get("total")),
        payment_terms=(data.get("payment_terms") or None) or None,
        notes=data.get("notes"),
        revision=data.get("revision"),
    )


def parse_xml(raw: str) -> Invoice:
    import xml.etree.ElementTree as ET

    root = ET.fromstring(raw)
    header = root.find("header")
    totals = root.find("totals")
    lines = []
    container = root.find("line_items")
    for item in list(container) if container is not None else []:
        line = make_line(
            item.findtext("name") or "",
            D(item.findtext("quantity")),
            D(item.findtext("unit_price")),
            D(item.findtext("amount")),
            item.findtext("note"),
        )
        if line:
            lines.append(line)
    due_raw = header.findtext("due_date") if header is not None else None
    date_raw = header.findtext("date") if header is not None else None
    number = header.findtext("invoice_number") if header is not None else None
    vendor = header.findtext("vendor") if header is not None else None
    currency = header.findtext("currency") if header is not None else None
    return Invoice(
        invoice_number=normalize_invoice_number(number),
        vendor_name=(vendor or "").strip(),
        invoice_date=parse_date(date_raw),
        due_date=parse_date(due_raw),
        invoice_date_raw=date_raw,
        due_date_raw=due_raw,
        currency=(currency or "USD").upper(),
        lines=lines,
        subtotal=D(totals.findtext("subtotal") if totals is not None else None),
        tax_rate=_rate(totals.findtext("tax_rate") if totals is not None else None),
        tax_amount=D(totals.findtext("tax_amount") if totals is not None else None),
        total=D(totals.findtext("total") if totals is not None else None),
        payment_terms=root.findtext("payment_terms"),
    )


def parse_csv(raw: str) -> Invoice:
    rows = [row for row in csv.reader(io.StringIO(raw)) if any(cell.strip() for cell in row)]
    if not rows:
        return Invoice()
    header = [cell.strip().lower() for cell in rows[0]]
    if header[:2] == ["field", "value"]:
        return _parse_kv_csv(rows[1:])
    return _parse_table_csv(header, rows[1:])


def _parse_kv_csv(rows: list[list[str]]) -> Invoice:
    meta: dict[str, str] = {}
    current: dict | None = None
    lines: list[LineItem] = []
    for row in rows:
        field = row[0].strip().lower()
        value = row[1].strip() if len(row) > 1 else ""
        if field == "item":
            current = {"name": value}
            continue
        if field in {"quantity", "qty"} and current is not None:
            current["quantity"] = value
            continue
        if field in {"unit_price", "price"} and current is not None:
            current["unit_price"] = value
            line = make_line(current["name"], D(current.get("quantity")), D(value))
            if line:
                lines.append(line)
            current = None
            continue
        meta[field] = value
    due_raw = meta.get("due_date")
    date_raw = meta.get("date")
    return Invoice(
        invoice_number=normalize_invoice_number(meta.get("invoice_number")),
        vendor_name=(meta.get("vendor") or "").strip(),
        invoice_date=parse_date(date_raw),
        due_date=parse_date(due_raw),
        invoice_date_raw=date_raw,
        due_date_raw=due_raw,
        currency=(meta.get("currency") or "USD").upper(),
        lines=lines,
        subtotal=D(meta.get("subtotal")),
        tax_amount=D(meta.get("tax") or meta.get("tax_amount")),
        total=D(meta.get("total")),
        payment_terms=meta.get("payment_terms"),
    )


def _parse_table_csv(header: list[str], rows: list[list[str]]) -> Invoice:
    def index(*names: str) -> int | None:
        for name in names:
            if name in header:
                return header.index(name)
        return None

    i_number = index("invoice number", "invoice_number")
    i_vendor = index("vendor")
    i_date = index("date")
    i_due = index("due date", "due_date")
    i_item = index("item")
    i_qty = index("qty", "quantity")
    i_price = index("unit price", "unit_price")
    i_amount = index("line total", "amount")
    lines: list[LineItem] = []
    number = vendor = date_raw = due_raw = None
    subtotal = tax_amount = total = None
    tax_rate = None
    for row in rows:
        def cell(idx: int | None) -> str:
            if idx is None or idx >= len(row):
                return ""
            return row[idx].strip()

        item = cell(i_item)
        if item:
            if number is None:
                number = cell(i_number)
                vendor = cell(i_vendor)
                date_raw = cell(i_date)
                due_raw = cell(i_due)
            line = make_line(item, D(cell(i_qty)), D(cell(i_price)), D(cell(i_amount)) or None)
            if line:
                lines.append(line)
            continue
        label, value = _summary_cells(row)
        if not label:
            continue
        if re.search(r"subtotal", label, re.IGNORECASE):
            subtotal = D(value)
        elif re.search(r"tax", label, re.IGNORECASE):
            tax_amount = D(value)
            rate_match = re.search(r"(\d+(?:\.\d+)?)\s*%", label)
            if rate_match:
                tax_rate = (Decimal(rate_match.group(1)) / Decimal("100")).quantize(Decimal("0.0001"))
        elif re.search(r"total", label, re.IGNORECASE):
            total = D(value)
    return Invoice(
        invoice_number=normalize_invoice_number(number),
        vendor_name=(vendor or "").strip(),
        invoice_date=parse_date(date_raw),
        due_date=parse_date(due_raw),
        invoice_date_raw=date_raw,
        due_date_raw=due_raw,
        lines=lines,
        subtotal=subtotal,
        tax_rate=tax_rate,
        tax_amount=tax_amount,
        total=total,
    )


def _summary_cells(row: list[str]) -> tuple[str, str]:
    cells = [cell.strip() for cell in row if cell.strip()]
    if len(cells) < 2:
        return "", ""
    return cells[-2], cells[-1]


def parse_text(raw: str) -> Invoice:
    fixed = fix_ocr(raw)
    meta: dict[str, str] = {}
    lines: list[LineItem] = []
    shipping = None
    tax_rate = None
    for original in fixed.splitlines():
        line = original.strip()
        if not line or set(line) <= set("-=_* "):
            continue
        if _consume_meta(line, meta):
            if "shipping" in meta and shipping is None:
                shipping = D(meta.pop("shipping"))
            if "tax_rate" in meta and tax_rate is None:
                tax_rate = Decimal(meta.pop("tax_rate"))
            continue
        item = _consume_item(line)
        if item:
            if item.kind == "charge":
                shipping = item.line_total or (item.quantity * item.unit_price)
                continue
            lines.append(item)
    due_raw = meta.get("due")
    date_raw = meta.get("date")
    return Invoice(
        invoice_number=normalize_invoice_number(meta.get("number")),
        vendor_name=(meta.get("vendor") or "").strip(),
        vendor_address=meta.get("address"),
        invoice_date=parse_date(date_raw),
        due_date=parse_date(due_raw),
        invoice_date_raw=date_raw,
        due_date_raw=due_raw,
        currency=(meta.get("currency") or "USD").upper(),
        lines=lines,
        subtotal=D(meta.get("subtotal")),
        tax_rate=tax_rate,
        tax_amount=D(meta.get("tax")),
        shipping=shipping,
        total=D(meta.get("total")),
        payment_terms=meta.get("terms"),
        notes=meta.get("notes"),
    )


# Longer labels first so "due date" wins over "due" and "invoice number" over "invoice".
IDENTITY = re.compile(
    r"(?P<label>invoice\s*number|payment\s+terms|pymnt\s+terms|due\s+date|due\s+dt|"
    r"inv\s*no\.?|inv\s*#|invoice|vendor|vndr|from|address|currency|notes?|"
    r"terms|date|dt|due|inv)\s*[:#]\s*",
    re.IGNORECASE,
)


def _label_key(label: str) -> str:
    key = re.sub(r"[.\s]+", " ", label.lower()).strip()
    if key in {"vendor", "vndr", "from"}:
        return "vendor"
    if key in {"invoice number", "invoice", "inv no", "inv #", "inv"}:
        return "number"
    if key in {"due date", "due dt", "due"}:
        return "due"
    if key in {"date", "dt"}:
        return "date"
    if key in {"payment terms", "pymnt terms", "terms"}:
        return "terms"
    if key == "currency":
        return "currency"
    if key == "address":
        return "address"
    if key.startswith("note"):
        return "notes"
    return key


def _identity_pairs(line: str) -> dict[str, str]:
    """Pull every Label: value pair off a line.

    PDF text sometimes places `Vendor:` and `Due:` on the same physical line.
    A greedy `Vendor: (?P<name>.+)$` would swallow the due date.
    """
    matches = list(IDENTITY.finditer(line))
    if not matches or matches[0].start() != 0:
        return {}
    pairs: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
        value = " ".join(line[match.end() : end].split())
        if value:
            pairs[_label_key(match.group("label"))] = value
    return pairs


def _consume_meta(line: str, meta: dict[str, str]) -> bool:
    pairs = _identity_pairs(line)
    if pairs:
        for key, value in pairs.items():
            if key == "vendor" and "@" in value:
                continue
            meta[key] = value
        return True
    sub = SUB_RE.match(line)
    if sub:
        meta["subtotal"] = sub.group("amt")
        return True
    ship = SHIP_RE.match(line)
    if ship:
        meta["shipping"] = ship.group("amt")
        return True
    tax = TAX_RE.match(line)
    if tax:
        meta["tax"] = tax.group("amt")
        if tax.group("rate") is not None:
            meta["tax_rate"] = str(Decimal(tax.group("rate")) / Decimal("100"))
        return True
    total = TOTAL_RE.match(line)
    if total:
        meta["total"] = total.group("amt")
        return True
    if re.fullmatch(r"(items|itms|items ordered)\s*:?", line, re.IGNORECASE):
        return True
    return False


def _consume_item(line: str) -> LineItem | None:
    # Table rows before the "x qty" pattern. Otherwise "Gadget X  4" is read as
    # a multiplier and the SKU collapses to "Gadget".
    for pattern in (ITEM_QTY, ITEM_TABLE, ITEM_X):
        match = pattern.match(line)
        if not match:
            continue
        name = match.group("name").strip()
        if name.lower() in {"item", "description", "items", "qty", "quantity"}:
            continue
        amount = match.groupdict().get("amount")
        note = match.groupdict().get("note")
        return make_line(name, D(match.group("qty")), D(match.group("price")), D(amount) if amount else None, note)
    return None


def _number_from_text(raw: str) -> str | None:
    match = NUMBER_ANYWHERE.search(raw)
    if match:
        return f"INV-{match.group(1)}"
    return None


def _rate(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    number = Decimal(str(value))
    return number


def draft_to_invoice(draft: InvoiceDraft, raw_text: str, source_path: str, source_format: str) -> Invoice:
    """Normalize a model draft with the same money and SKU rules as the readers."""
    lines = []
    for row in draft.lines:
        line = make_line(row.name, D(row.quantity), D(row.unit_price), D(row.line_total), row.note)
        if line:
            lines.append(line)
    due_raw = draft.due_date
    date_raw = draft.invoice_date
    return Invoice(
        invoice_number=normalize_invoice_number(draft.invoice_number),
        vendor_name=(draft.vendor_name or "").strip(),
        vendor_address=draft.vendor_address,
        invoice_date=parse_date(date_raw),
        due_date=parse_date(due_raw),
        invoice_date_raw=date_raw,
        due_date_raw=due_raw,
        currency=(draft.currency or "USD").upper(),
        lines=lines,
        subtotal=D(draft.subtotal),
        tax_rate=_rate(draft.tax_rate) if draft.tax_rate else None,
        tax_amount=D(draft.tax_amount),
        shipping=D(draft.shipping),
        total=D(draft.total),
        payment_terms=draft.payment_terms,
        notes=draft.notes,
        revision=draft.revision,
        raw_text=raw_text,
        source_path=source_path,
        source_format=source_format,
    )


def drafts_agree(left: Invoice, right: Invoice) -> bool:
    if left.invoice_number != right.invoice_number:
        return False
    if (left.vendor_name or "").strip().lower() != (right.vendor_name or "").strip().lower():
        return False
    if left.total != right.total:
        return False
    left_lines = sorted((line.sku or line.raw_name, str(line.quantity), str(line.unit_price)) for line in left.lines)
    right_lines = sorted(
        (line.sku or line.raw_name, str(line.quantity), str(line.unit_price)) for line in right.lines
    )
    return left_lines == right_lines


def peek_invoice_number(path: Path) -> str | None:
    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            return normalize_invoice_number(str(data.get("invoice_number") or ""))
        text = read_pdf(path) if suffix == ".pdf" else read_text(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    found = _number_from_text(text)
    if found:
        return found
    match = re.search(r"(?:inv(?:oice)?\s*(?:number|#|no\.?)?)\s*[:#]?\s*(\d{4})", text, re.IGNORECASE)
    if match:
        return f"INV-{match.group(1)}"
    return None
