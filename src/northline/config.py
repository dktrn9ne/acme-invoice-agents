"""Runtime configuration. A missing API key is a valid mode, not an error."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = "grok-4.3"


def load_env() -> None:
    """Load .env from the repo root and the working directory. Never overrides real env."""
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(Path.cwd() / ".env", override=False)


@dataclass(frozen=True)
class Settings:
    offline: bool
    model: str
    api_key: str | None
    db_path: Path
    checkpoint_path: Path
    log_path: Path
    timeout_seconds: float
    high_value: Decimal = Decimal("10000")
    price_variance: Decimal = Decimal("0.05")
    threshold_window: Decimal = Decimal("500")
    hitl: bool = False

    @property
    def mode(self) -> str:
        return "offline" if self.offline else "live"


def resolve_settings(
    *,
    offline: bool | None = None,
    live: bool = False,
    db_path: Path | None = None,
    log_path: Path | None = None,
    hitl: bool = False,
) -> Settings:
    load_env()
    key = (os.environ.get("XAI_API_KEY") or "").strip() or None
    model = (os.environ.get("GROK_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if live and offline:
        raise ValueError("Choose either --live or --offline, not both.")
    if live and not key:
        raise RuntimeError(
            "Live mode needs XAI_API_KEY in the environment or a .env file. "
            "Run without --live to use the offline reasoner."
        )
    use_offline = True if offline else (False if live else key is None)
    db = Path(db_path or os.environ.get("NORTHLINE_DB") or (ROOT / "data" / "northline.db"))
    log = Path(log_path or os.environ.get("NORTHLINE_LOG") or (ROOT / "logs" / "northline.jsonl"))
    timeout = float(os.environ.get("NORTHLINE_TIMEOUT_SECONDS") or "45")
    return Settings(
        offline=use_offline,
        model=model,
        api_key=None if use_offline else key,
        db_path=db,
        checkpoint_path=db.with_name("checkpoints.sqlite"),
        log_path=log,
        timeout_seconds=timeout,
        hitl=hitl,
    )
