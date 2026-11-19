"""SQLite ledger: item master, vendors, payments, review queue, audit trail."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory (
    sku TEXT PRIMARY KEY,
    item_name TEXT NOT NULL,
    stock INTEGER NOT NULL,
    unit_price TEXT,
    currency TEXT NOT NULL DEFAULT 'USD'
);

CREATE TABLE IF NOT EXISTS vendors (
    name TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    notes TEXT
);

CREATE TABLE IF NOT EXISTS vendor_aliases (
    alias TEXT PRIMARY KEY,
    vendor_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_number TEXT NOT NULL,
    vendor TEXT,
    amount TEXT NOT NULL,
    currency TEXT NOT NULL,
    source_path TEXT,
    run_id TEXT,
    status TEXT NOT NULL,
    confirmation TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    thread_id TEXT,
    invoice_number TEXT,
    vendor TEXT,
    amount TEXT,
    currency TEXT,
    source_path TEXT,
    source_name TEXT,
    reasons TEXT NOT NULL,
    status TEXT NOT NULL,
    reviewer_note TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    thread_id TEXT,
    invoice_number TEXT,
    source_path TEXT,
    agent TEXT NOT NULL,
    stage TEXT NOT NULL,
    message TEXT NOT NULL,
    payload TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    thread_id TEXT,
    source_path TEXT,
    source_name TEXT,
    mode TEXT NOT NULL,
    invoice_number TEXT,
    vendor TEXT,
    currency TEXT,
    total TEXT,
    decision TEXT,
    flags_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def dumps(value: object) -> str:
    return json.dumps(value, default=str, sort_keys=True)
