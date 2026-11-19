"""JSON-lines log on stderr, plus an append-only file. The rich summary stays on stdout."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from northline.db import utcnow


def configure_logging() -> None:
    logger = logging.getLogger("northline")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if any(getattr(handler, "_northline", False) for handler in logger.handlers):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler._northline = True  # type: ignore[attr-defined]
    logger.addHandler(handler)


class JsonlLog:
    def __init__(self, path: Path | None, run_id: str):
        self.path = path
        self.run_id = run_id
        self.logger = logging.getLogger("northline")
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, agent: str, message: str, **fields) -> dict:
        record = {
            "ts": utcnow(),
            "level": "info",
            "run_id": self.run_id,
            "agent": agent,
            "message": message,
            **fields,
        }
        line = json.dumps(record, default=str)
        self.logger.info(line)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return record

    def write_graph_event(self, event: dict) -> None:
        self.event(
            event.get("agent") or "desk",
            event.get("message") or "",
            stage=event.get("stage"),
            invoice_number=event.get("invoice_number"),
            payload=event.get("payload") or {},
        )
