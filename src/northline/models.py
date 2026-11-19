"""Invoice shapes shared by parsers, the graph, and the Grok schema."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LineItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    raw_name: str
    sku: str | None = None
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal | None = None
    note: str | None = None
    kind: Literal["goods", "charge"] = "goods"


class Invoice(BaseModel):
    """Normalized invoice. Money is Decimal; dump with model_dump(mode='json') for the graph."""

    model_config = ConfigDict(extra="ignore")

    invoice_number: str | None = None
    vendor_name: str | None = None
    vendor_address: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    invoice_date_raw: str | None = None
    due_date_raw: str | None = None
    currency: str = "USD"
    lines: list[LineItem] = Field(default_factory=list)
    subtotal: Decimal | None = None
    tax_rate: Decimal | None = None
    tax_amount: Decimal | None = None
    shipping: Decimal | None = None
    total: Decimal | None = None
    payment_terms: str | None = None
    notes: str | None = None
    revision: str | None = None
    raw_text: str = ""
    source_path: str = ""
    source_format: str = ""
    extraction_notes: list[str] = Field(default_factory=list)
    extraction_diff: list[dict] = Field(default_factory=list)

    def state(self) -> dict:
        return self.model_dump(mode="json")


class DraftLine(BaseModel):
    """Loose line the model fills. Strings, because messy documents are not numbers yet."""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    quantity: str = ""
    unit_price: str = ""
    line_total: str | None = None
    note: str | None = None


class InvoiceDraft(BaseModel):
    """Structured-output schema sent to Grok for messy text. Do not reconcile totals here."""

    model_config = ConfigDict(extra="ignore")

    invoice_number: str | None = None
    vendor_name: str | None = None
    vendor_address: str | None = None
    invoice_date: str | None = None
    due_date: str | None = None
    currency: str | None = None
    lines: list[DraftLine] = Field(default_factory=list)
    subtotal: str | None = None
    tax_rate: str | None = None
    tax_amount: str | None = None
    shipping: str | None = None
    total: str | None = None
    payment_terms: str | None = None
    notes: str | None = None
    revision: str | None = None


class Narrative(BaseModel):
    decision: Literal["paid", "rejected", "needs_review"]
    rationale: str
    concerns: list[str] = Field(default_factory=list)


class Critique(BaseModel):
    verdict: Literal["accept", "revise"]
    notes: str
    recommended_decision: Literal["paid", "rejected", "needs_review"]


def schema_errors(invoice: Invoice) -> list[str]:
    """Extraction failures, not business failures.

    A blank vendor or a total that disagrees with the lines is a real invoice
    problem and must survive into validation. This only checks that we parsed
    a document shape at all.
    """
    errors: list[str] = []
    if not invoice.invoice_number:
        errors.append("invoice_number missing")
    if not invoice.lines:
        errors.append("no line items parsed")
    for index, line in enumerate(invoice.lines, start=1):
        if line.quantity is None:
            errors.append(f"line {index} quantity missing")
        if line.unit_price is None and line.kind == "goods":
            errors.append(f"line {index} unit price missing")
    return errors
