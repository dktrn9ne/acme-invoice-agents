from pathlib import Path

from northline.critique import enforce_critique, names_high_value_threshold, rationale_contradicts
from northline.models import Critique, Narrative
from northline.eval import decision_exit
from northline.extract import parse_document
from northline.validate import arithmetic_facts

INVOICES = Path(__file__).resolve().parents[1] / "data" / "invoices"


def test_1004_false_tax_claim_contradicts_the_controls():
    facts = arithmetic_facts(parse_document(INVOICES / "invoice_1004.json"))
    assert facts["total_matches"] is True
    assert facts["line_sum"] == "1750.00"
    assert facts["stated_tax"] == "140.00"
    assert facts["expected_total"] == "1890.00"
    note = rationale_contradicts(
        "Invoice total 1890 does not match line items (3*250 + 2*500 = 1750)",
        facts,
    )
    assert note is not None
    assert "140.00" in note
    assert rationale_contradicts(
        "Stated total 1890.00 includes tax of 140.00 on a line sum of 1750.00. Pay.",
        facts,
    ) is None


def test_a_real_mismatch_is_not_called_a_contradiction():
    facts = arithmetic_facts(parse_document(INVOICES / "invoice_1007.csv"))
    assert facts["total_matches"] is False
    assert rationale_contradicts("The printed totals do not match the lines.", facts) is None
    assert rationale_contradicts("The totals match and the arithmetic is clean.", facts) is not None


def test_high_value_threshold_accepts_common_forms():
    accepted = ["$10k", "10K", "$10,000", "10000", "ten thousand", "10,000", "$10k+", "($10k+)", "above >$10k"]
    for text in accepted:
        assert names_high_value_threshold(text), text
    assert not names_high_value_threshold("the total is $9,975")
    assert not names_high_value_threshold("about 1000 dollars")
    dossier = {
        "policy_decision": "rejected",
        "flags": [{"code": "high_value", "severity": "info"}],
        "arithmetic": {"total_matches": True, "tax_matches": True, "subtotal_matches_lines": True},
    }
    accept = Critique(verdict="accept", notes="ok", recommended_decision="rejected")
    shorthand = enforce_critique(
        dossier,
        Narrative(decision="rejected", rationale="Over $10k. Do not pay.", concerns=[]),
        accept,
    )
    assert shorthand.verdict == "accept"
    missing = enforce_critique(
        dossier,
        Narrative(decision="rejected", rationale="This invoice is large.", concerns=[]),
        accept,
    )
    assert missing.verdict == "revise"


def test_live_exit_is_zero_unless_a_threshold_is_set():
    summary = {"decision_accuracy": 0.95, "flag_f1": 1.0, "extraction_ok": True}
    assert decision_exit(summary, live=True, min_accuracy=None) == 0
    assert decision_exit(summary, live=True, min_accuracy=0.95) == 0
    assert decision_exit(summary, live=True, min_accuracy=1) == 1
    assert decision_exit(summary, live=False, min_accuracy=None) == 1
