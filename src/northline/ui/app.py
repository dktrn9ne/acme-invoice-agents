"""Local FastAPI desk. Offline by default. Live only when the form asks and a key exists."""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from northline.config import ROOT, resolve_settings
from northline.db import connect, ensure_schema
from northline.desk import resolve_review
from northline.extract import read_pdf, read_text
from northline.runner import run_path
from northline.seed import seed

HERE = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))
READABLE = {".txt", ".pdf", ".json", ".csv", ".xml"}


def format_money(value: object) -> str:
    if value is None or value == "":
        return "—"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    sign = "−" if number < 0 else ""
    return f"{sign}${abs(number):,.2f}"


def decision_label(value: object) -> str:
    return {"paid": "Paid", "rejected": "Blocked", "needs_review": "In review"}.get(str(value), str(value or "—"))


def stage_label(value: object) -> str:
    return {
        "ingest": "Read",
        "correct": "Correct",
        "validate": "Check",
        "approve": "Draft",
        "critique": "Critique",
        "pay": "Settle",
        "review": "Review",
    }.get(str(value), str(value or ""))


TEMPLATES.env.filters["money"] = format_money
TEMPLATES.env.filters["decision"] = decision_label
TEMPLATES.env.filters["stage"] = stage_label


@contextmanager
def session(path: Path):
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


def create_app(db_path: Path | str | None = None) -> FastAPI:
    path = Path(db_path) if db_path else ROOT / "data" / "northline.db"
    prepare(path)
    app = FastAPI(title="Northline")
    app.state.db_path = path
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def shell(page: str, **extra) -> dict:
        with session(path) as conn:
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM review_queue WHERE status = 'pending'"
            ).fetchone()["n"]
        return {"page": page, "pending": pending, **extra}

    @app.get("/")
    def dashboard(request: Request):
        with session(path) as conn:
            stats = summarize(conn)
            runs = load_runs(conn)[:24]
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            shell("desk", runs=runs, **stats),
        )

    @app.get("/invoices")
    def invoices(request: Request, error: str = ""):
        with session(path) as conn:
            runs = load_runs(conn)[:20]
        return TEMPLATES.TemplateResponse(
            request,
            "invoices.html",
            shell("invoices", samples=sample_names(), runs=runs, error=error),
        )

    @app.post("/invoices/run")
    async def run_invoice(
        sample: str = Form(""),
        mode: str = Form("offline"),
        file: UploadFile | None = File(None),
    ):
        target: Path | None = None
        if file is not None and file.filename:
            suffix = Path(file.filename).suffix.lower()
            if suffix not in READABLE:
                return RedirectResponse("/invoices?error=Unsupported+file+type", status_code=303)
            folder = ROOT / "var" / "uploads" / uuid.uuid4().hex
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / Path(file.filename).name
            target.write_bytes(await file.read())
        elif sample:
            target = ROOT / "data" / "invoices" / Path(sample).name
            if not target.exists() or target.suffix.lower() not in READABLE:
                return RedirectResponse("/invoices?error=Unknown+sample", status_code=303)
        else:
            return RedirectResponse("/invoices?error=Choose+a+sample+or+upload+a+file", status_code=303)
        try:
            settings = resolve_settings(
                offline=True if mode != "live" else None,
                live=mode == "live",
                db_path=path,
                log_path=ROOT / "logs" / "ui.jsonl",
            )
            result = run_path(target, settings)
        except Exception as exc:
            message = str(exc).replace(" ", "+")
            return RedirectResponse(f"/invoices?error={message}", status_code=303)
        return RedirectResponse(f"/runs/{result['run_id']}", status_code=303)

    @app.get("/runs/{run_id}")
    def detail(request: Request, run_id: str):
        with session(path) as conn:
            run = get_run(conn, run_id)
        if run is None:
            return RedirectResponse("/invoices?error=That+run+is+not+on+the+desk", status_code=303)
        result = run["result"]
        invoice = result.get("invoice") or {}
        source_path = run.get("source_path") or ""
        return TEMPLATES.TemplateResponse(
            request,
            "detail.html",
            shell(
                "invoices",
                run=run,
                invoice=invoice,
                flags=result.get("flags") or [],
                events=result.get("events") or [],
                payment=result.get("payment") or {},
                rationale=result.get("draft_rationale") or result.get("policy_rationale") or "",
                source=read_source(source_path),
            ),
        )

    @app.get("/review")
    def review(request: Request, error: str = ""):
        with session(path) as conn:
            items = load_queue(conn)
        return TEMPLATES.TemplateResponse(
            request,
            "review.html",
            shell("review", items=items, error=error),
        )

    @app.post("/review/{run_id}")
    def resolve(run_id: str, action: str = Form(...), note: str = Form("")):
        try:
            with session(path) as conn:
                resolve_review(conn, run_id, action, note.strip())
        except Exception as exc:
            message = str(exc).replace(" ", "+")
            return RedirectResponse(f"/review?error={message}", status_code=303)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.get("/audit")
    def audit(request: Request, agent: str = ""):
        with session(path) as conn:
            events = load_events(conn, agent or None)
            agents = [
                row["agent"]
                for row in conn.execute(
                    "SELECT DISTINCT agent FROM audit_events ORDER BY agent"
                ).fetchall()
            ]
        return TEMPLATES.TemplateResponse(
            request,
            "audit.html",
            shell("audit", events=events, agents=agents, agent=agent),
        )

    return app


def prepare(path: Path) -> None:
    conn = connect(path)
    try:
        ensure_schema(conn)
        count = conn.execute("SELECT COUNT(*) AS n FROM inventory").fetchone()["n"]
        if count == 0:
            seed(conn)
    finally:
        conn.close()


def load_runs(conn) -> list[dict]:
    rows = []
    for row in conn.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall():
        item = dict(row)
        item["result"] = json.loads(item["result_json"])
        item["flags"] = json.loads(item["flags_json"])
        rows.append(item)
    return rows


def get_run(conn, run_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["result"] = json.loads(item["result_json"])
    item["flags"] = json.loads(item["flags_json"])
    return item


def load_queue(conn) -> list[dict]:
    items = []
    for row in conn.execute(
        "SELECT * FROM review_queue WHERE status = 'pending' ORDER BY created_at DESC"
    ).fetchall():
        item = dict(row)
        item["reasons"] = json.loads(item["reasons"])
        items.append(item)
    return items


def load_events(conn, agent: str | None) -> list[dict]:
    if agent:
        rows = conn.execute(
            """
            SELECT * FROM audit_events
            WHERE agent = ?
            ORDER BY id DESC LIMIT 250
            """,
            (agent,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT 250").fetchall()
    return [dict(row) for row in rows]


def summarize(conn) -> dict:
    paid = Decimal("0")
    for row in conn.execute("SELECT amount FROM payments WHERE status = 'success'").fetchall():
        paid += Decimal(row["amount"])
    blocked = Decimal("0")
    waiting = Decimal("0")
    hits = 0
    for row in conn.execute("SELECT decision, total, flags_json FROM runs").fetchall():
        flags = json.loads(row["flags_json"])
        hits += sum(1 for flag in flags if flag.get("severity") in {"block", "review"})
        try:
            total = Decimal(row["total"]) if row["total"] not in (None, "") else Decimal("0")
        except InvalidOperation:
            total = Decimal("0")
        if row["decision"] == "rejected" and total > 0:
            blocked += total
        elif row["decision"] == "needs_review" and total > 0:
            waiting += total
    return {"paid": paid, "blocked": blocked, "waiting": waiting, "hits": hits}


def sample_names() -> list[str]:
    folder = ROOT / "data" / "invoices"
    return sorted(item.name for item in folder.iterdir() if item.suffix.lower() in READABLE)


def read_source(source_path: str) -> str:
    if not source_path:
        return ""
    path = Path(source_path)
    if not path.exists():
        return ""
    try:
        text = read_pdf(path) if path.suffix.lower() == ".pdf" else read_text(path)
    except Exception as exc:
        return f"(Could not read the file: {exc.__class__.__name__})"
    if len(text) > 12000:
        return text[:12000] + "\n…"
    return text


app = create_app()
