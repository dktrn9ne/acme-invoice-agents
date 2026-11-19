"""Create the local item master.

The four stock rows are the case seed. Unit prices, the approved-vendor
list, and aliases are extensions this desk needs for price and vendor checks.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from northline.db import connect, ensure_schema

# Case seed: WidgetA 15, WidgetB 10, GadgetX 5, FakeItem 0.
# Prices are the catalog the clean invoices actually bill. FakeItem has no price.
INVENTORY = [
    ("WidgetA", "Widget A", 15, "250.00", "USD"),
    ("WidgetB", "Widget B", 10, "500.00", "USD"),
    ("GadgetX", "Gadget X", 5, "750.00", "USD"),
    ("FakeItem", "Fake Item", 0, None, "USD"),
]

# status: approved vendors may be paid when every other control is clean.
# Anyone else is unapproved and goes to a person (or is rejected for a harder flag).
VENDORS = [
    ("Widgets Inc.", "approved", "USD", "Catalog vendor"),
    ("Gadgets Co.", "approved", "USD", "Catalog vendor"),
    ("Precision Parts Ltd.", "approved", "USD", "Catalog vendor"),
    ("Acme Industrial Supplies", "approved", "USD", "Catalog vendor"),
    ("MegaWidgets Corp", "approved", "USD", "Catalog vendor"),
    ("Consolidated Materials Group", "approved", "USD", "Catalog vendor"),
    ("Summit Manufacturing Co.", "approved", "USD", "Catalog vendor"),
    ("QuickShip Distributers", "approved", "USD", "Name on current invoices"),
    ("Atlas Industrial Supply", "approved", "USD", "Catalog vendor"),
    ("TechParts International", "approved", "EUR", "Bills in EUR; held for FX"),
    ("Reliable Components Inc.", "approved", "USD", "Catalog vendor"),
]

ALIASES = [
    ("FastShip Ltd.", "QuickShip Distributers"),
    ("Widget A", "Widgets Inc."),
]


def seed(conn) -> None:
    ensure_schema(conn)
    conn.executemany(
        """
        INSERT INTO inventory (sku, item_name, stock, unit_price, currency)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(sku) DO UPDATE SET
            item_name = excluded.item_name,
            stock = excluded.stock,
            unit_price = excluded.unit_price,
            currency = excluded.currency
        """,
        INVENTORY,
    )
    conn.executemany(
        """
        INSERT INTO vendors (name, status, currency, notes)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            status = excluded.status,
            currency = excluded.currency,
            notes = excluded.notes
        """,
        VENDORS,
    )
    conn.executemany(
        """
        INSERT INTO vendor_aliases (alias, vendor_name)
        VALUES (?, ?)
        ON CONFLICT(alias) DO UPDATE SET vendor_name = excluded.vendor_name
        """,
        ALIASES,
    )
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the Northline SQLite item master.")
    parser.add_argument("--db", default="data/northline.db", help="SQLite path to create.")
    args = parser.parse_args(argv)
    path = Path(args.db)
    conn = connect(path)
    try:
        seed(conn)
    finally:
        conn.close()
    print(f"Seeded item master at {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
