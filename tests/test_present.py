import os
import time

from northline.ui.present import format_local, humanize_event


def test_validation_step_is_a_sentence_and_the_raw_log_stays_put():
    raw = (
        "Controls recommend rejected. Tools used: find_payment, lookup_sku, lookup_vendor. "
        "Arithmetic: line sum 100000.00, tax 0.00, expected total 100000.00, "
        "stated total 100000.00, total_matches True."
    )
    event = {
        "stage": "validate",
        "message": raw,
        "payload": {
            "tools": [
                {"tool": "lookup_vendor"},
                {"tool": "lookup_sku"},
                {"tool": "find_payment"},
            ],
            "arithmetic": {
                "line_sum": "100000.00",
                "stated_tax": "0.00",
                "expected_total": "100000.00",
                "stated_total": "100000.00",
                "total_matches": True,
            },
        },
    }
    shown = humanize_event(event)
    assert "lookup_sku" not in shown
    assert "total_matches" not in shown
    assert "$100,000.00" in shown
    assert "blocking payment" in shown
    assert event["message"] == raw


def test_flag_codes_in_a_draft_become_words():
    shown = humanize_event(
        {
            "stage": "approve",
            "message": "Draft decision rejected. Flags: unapproved_vendor, due_date_invalid. Do not pay.",
            "payload": {},
        }
    )
    assert "unapproved_vendor" not in shown
    assert "Unapproved vendor" in shown
    assert "Invalid due date" in shown


def test_local_time_names_the_zone(monkeypatch):
    monkeypatch.setenv("TZ", "America/Chicago")
    time.tzset()
    shown = format_local("2026-11-19T02:28:00+00:00")
    assert shown.startswith("2026-11-18 20:28")
    assert "CST" in shown or "CDT" in shown
    os.environ["TZ"] = "UTC"
    time.tzset()
