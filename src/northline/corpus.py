"""Sibling-file signals: revisions and the same invoice saved twice."""

from __future__ import annotations

from pathlib import Path

from northline.extract import peek_invoice_number
from northline.flags import Flag, make_flag

READABLE = {".txt", ".pdf", ".json", ".csv", ".xml"}


def related_documents(path: Path, invoice_number: str | None) -> list[Path]:
    if not invoice_number:
        return []
    parent = path.parent
    if not parent.exists():
        return []
    me = path.resolve()
    found: list[Path] = []
    for other in sorted(parent.iterdir()):
        if not other.is_file() or other.suffix.lower() not in READABLE:
            continue
        if other.resolve() == me:
            continue
        if peek_invoice_number(other) == invoice_number:
            found.append(other)
    return found


def corpus_flags(path: Path, invoice_number: str | None, revision: str | None) -> list[Flag]:
    """Flag the revision, not the original, so INV-1004 itself stays a clean pass.

    Identical twins (pdf + txt, pdf + json) are noted on both copies. Payment
    idempotency, not this flag, is what stops the second copy from being paid
    again once the first copy has settled.
    """
    related = related_documents(path, invoice_number)
    if not related and not revision and "revised" not in path.name.lower():
        return []
    self_revised = "revised" in path.name.lower() or bool(revision)
    flags: list[Flag] = []
    if self_revised:
        names = ", ".join(item.name for item in related) or "no earlier copy in this folder"
        flags.append(
            make_flag(
                "revision",
                f"{path.name} is marked as a revision ({revision or 'filename'}).",
                revision=revision,
            )
        )
        flags.append(
            make_flag(
                "duplicate_invoice",
                f"{invoice_number} is already on file ({names}). Paying both would double-pay.",
                siblings=[item.name for item in related],
            )
        )
        return flags
    other_formats = [
        item
        for item in related
        if item.suffix.lower() != path.suffix.lower() and "revised" not in item.name.lower()
    ]
    if other_formats:
        suffixes = ", ".join(sorted({item.suffix.lower() for item in other_formats}))
        flags.append(
            make_flag(
                "cross_format_duplicate",
                f"{invoice_number} is also stored as {suffixes}. The ledger pays this number once.",
                siblings=[item.name for item in other_formats],
            )
        )
    return flags
