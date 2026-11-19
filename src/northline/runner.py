"""Run one document, or a folder, through the desk."""

from __future__ import annotations

import uuid
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from northline.config import Settings
from northline.db import connect, ensure_schema
from northline.desk import save_events, save_run
from northline.extract import read_pdf, read_text
from northline.graph import build_graph
from northline.logging_utils import JsonlLog, configure_logging
from northline.reasoners import GrokReasoner, OfflineReasoner, Reasoner
from northline.seed import seed
from northline.validate import Ledger

READABLE = {".txt", ".pdf", ".json", ".csv", ".xml"}


def build_reasoner(settings: Settings) -> Reasoner:
    if settings.offline or not settings.api_key:
        return OfflineReasoner()
    return GrokReasoner(settings.model, settings.api_key, settings.timeout_seconds)


def run_path(
    path: Path | str,
    settings: Settings,
    *,
    reasoner: Reasoner | None = None,
    conn=None,
    checkpointer=None,
    run_id: str | None = None,
) -> dict:
    configure_logging()
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    if path.suffix.lower() not in READABLE:
        raise ValueError(f"Unsupported invoice type: {path.suffix}")
    own_conn = conn is None
    if own_conn:
        conn = connect(settings.db_path)
    try:
        ensure_schema(conn)
        if conn.execute("SELECT COUNT(*) AS n FROM inventory").fetchone()["n"] == 0:
            seed(conn)
        reasoner = reasoner or build_reasoner(settings)
        run_id = run_id or uuid.uuid4().hex
        raw = read_pdf(path) if path.suffix.lower() == ".pdf" else read_text(path)
        initial = {
            "run_id": run_id,
            "thread_id": run_id,
            "source_path": str(path.resolve()),
            "source_name": path.name,
            "mode": settings.mode if reasoner.__class__.__name__ in {"OfflineReasoner", "GrokReasoner"} else getattr(reasoner, "name", settings.mode),
            "hitl": settings.hitl,
            "raw_text": raw,
            "source_format": path.suffix.lower().lstrip("."),
            "feedback": [],
            "extract_attempts": 0,
            "critique_attempts": 0,
            "events": [],
        }
        ledger = Ledger(conn)
        sink = JsonlLog(settings.log_path, run_id)
        sink.event("desk", f"Starting {path.name}", mode=initial["mode"], model=settings.model)
        result = _invoke(reasoner, ledger, initial, settings, checkpointer, resume=None)
        for event in result.get("events") or []:
            sink.write_graph_event(event)
        if result.get("paused"):
            sink.event("desk", "Paused for a person", invoice_number=(result.get("invoice") or {}).get("invoice_number"))
        save_events(conn, result.get("events") or [])
        save_run(conn, result)
        sink.event(
            "desk",
            f"Finished {path.name}: {result.get('final_decision')}",
            decision=result.get("final_decision"),
            invoice_number=(result.get("invoice") or {}).get("invoice_number"),
            flags=[flag["code"] for flag in result.get("flags") or []],
        )
        return result
    finally:
        if own_conn:
            conn.close()


def resume_path(
    settings: Settings,
    thread_id: str,
    action: str,
    note: str = "",
    *,
    conn=None,
    checkpointer=None,
) -> dict:
    """Continue a run that interrupted for a person. The checkpointer must be the sqlite file."""
    configure_logging()
    own_conn = conn is None
    if own_conn:
        conn = connect(settings.db_path)
    try:
        ensure_schema(conn)
        reasoner = build_reasoner(settings)
        ledger = Ledger(conn)
        result = _invoke(
            reasoner,
            ledger,
            {},
            settings,
            checkpointer=checkpointer,
            resume={"action": action, "note": note},
            thread_id=thread_id,
        )
        save_events(conn, [event for event in result.get("events") or [] if event.get("stage") in {"pay", "review"}][-1:])
        save_run(conn, result)
        return result
    finally:
        if own_conn:
            conn.close()


def _invoke(reasoner, ledger, initial, settings: Settings, checkpointer, resume, thread_id: str | None = None) -> dict:
    config = {"configurable": {"thread_id": thread_id or initial["run_id"]}}

    def once(saver):
        graph = build_graph(reasoner, ledger, saver)
        if resume is None:
            result = graph.invoke(initial, config)
        else:
            result = graph.invoke(Command(resume=resume), config)
        return _normalize(result, initial)

    if checkpointer is not None:
        return once(checkpointer)
    settings.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(settings.checkpoint_path)) as saver:
        return once(saver)


def _normalize(result: dict, initial: dict) -> dict:
    paused = bool(result.get("__interrupt__"))
    merged = dict(initial)
    merged.update({key: value for key, value in result.items() if key != "__interrupt__"})
    merged["paused"] = paused
    if paused:
        merged["final_decision"] = merged.get("final_decision") or "needs_review"
        merged["payment"] = {"status": "not_paid", "reason": "paused_for_review"}
    merged.setdefault("payment", {"status": "not_paid"})
    merged.setdefault("flags", [])
    merged.setdefault("events", [])
    return merged


def discover(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(item for item in path.iterdir() if item.is_file() and item.suffix.lower() in READABLE)


def memory_saver():
    return MemorySaver()
