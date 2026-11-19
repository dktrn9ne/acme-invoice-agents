"""The only function that stands in for the bank."""

from __future__ import annotations


def mock_payment(vendor: str, amount) -> dict:
    """Pay a vendor. The case specifies this shape; do not add network calls here."""
    print(f"Paid {amount} to {vendor}")
    return {"status": "success"}
