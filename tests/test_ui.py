from __future__ import annotations

from fastapi.testclient import TestClient

from northline.ui.app import create_app


def test_desk_runs_a_sample_and_a_person_can_release_a_hold(tmp_path):
    client = TestClient(create_app(tmp_path / "desk.db"))
    home = client.get("/")
    assert home.status_code == 200
    assert "Northline" in home.text
    assert "Money moves when the desk is" in home.text

    paid = client.post("/invoices/run", data={"sample": "invoice_1001.txt", "mode": "offline"})
    assert paid.status_code == 200
    assert "INV-1001" in paid.text
    assert "Paid" in paid.text
    assert "Widgets Inc." in paid.text
    assert "Ingestion" in paid.text or "ingestion" in paid.text

    held = client.post("/invoices/run", data={"sample": "invoice_1010.txt", "mode": "offline"})
    assert "In review" in held.text
    assert "price_variance" in held.text
    run_id = held.url.path.rstrip("/").split("/")[-1]

    queue = client.get("/review")
    assert queue.status_code == 200
    assert "INV-1010" in queue.text

    released = client.post(f"/review/{run_id}", data={"action": "approve", "note": "PO covers the rush price."})
    assert released.status_code == 200
    assert "NL-INV-1010" in released.text

    audit = client.get("/audit")
    assert "lookup" not in audit.text or "validation" in audit.text.lower() or "Validation" in audit.text or "validation" in audit.text
    assert "INV-1001" in audit.text

    again = client.get("/")
    assert "$5,000.00" in again.text
    css = client.get("/static/app.css")
    assert css.status_code == 200
    assert "Fraunces" in css.text
