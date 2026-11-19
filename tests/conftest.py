from __future__ import annotations

from pathlib import Path

import pytest

from northline.config import Settings
from northline.db import connect
from northline.seed import seed

ROOT = Path(__file__).resolve().parents[1]
INVOICES = ROOT / "data" / "invoices"


@pytest.fixture
def conn(tmp_path):
    database = connect(tmp_path / "ledger.db")
    seed(database)
    yield database
    database.close()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        offline=True,
        model="grok-4.3",
        api_key=None,
        db_path=tmp_path / "ledger.db",
        checkpoint_path=tmp_path / "checkpoints.sqlite",
        log_path=tmp_path / "northline.jsonl",
        timeout_seconds=5,
    )
