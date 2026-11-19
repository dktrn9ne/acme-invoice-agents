from __future__ import annotations

from northline.eval import aggregate, load_expectations, run_eval


def test_golden_file_covers_the_whole_corpus():
    expected = load_expectations()
    names = [row["file"] for row in expected["invoices"]]
    assert len(names) == 20
    assert len(set(names)) == 20


def test_offline_eval_matches_the_golden_set():
    rows, summary = run_eval(load_expectations(), live=False)
    failures = [row for row in rows if not (row["decision_ok"] and row["flags_ok"] and row["total_ok"] and row["vendor_ok"])]
    assert failures == []
    assert summary["decision_accuracy"] == 1
    assert summary["flag_f1"] == 1
    assert summary == aggregate(rows)
