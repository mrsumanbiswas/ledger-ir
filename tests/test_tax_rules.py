"""Date-dependent GST rate sets and tax-head rule (SPEC.md §1.1–1.2):
the allowed rate set switches on 2025-09-22."""
from functions.ast_compiler.core import tax_rules as tr

PRE = frozenset({0, 500, 1200, 1800, 2800, 300, 25})
POST = frozenset({0, 500, 1800, 4000, 300, 25})


def test_pre_switch_rate_set():
    assert tr.allowed_rates_bps("2025-09-21") == PRE


def test_post_switch_rate_set():
    assert tr.allowed_rates_bps("2025-09-22") == POST
    assert tr.allowed_rates_bps("2026-10-18") == POST


def test_12_and_28_pct_dropped_40_added_after_switch():
    assert 1200 in tr.allowed_rates_bps("2025-09-21")
    assert 1200 not in tr.allowed_rates_bps("2026-10-18")
    assert 2800 not in tr.allowed_rates_bps("2026-10-18")
    assert 4000 in tr.allowed_rates_bps("2026-10-18")
    assert 4000 not in tr.allowed_rates_bps("2025-09-21")


def test_special_rates_survive_switch():
    for d in ("2025-09-21", "2026-10-18"):
        assert {300, 25} <= tr.allowed_rates_bps(d)


def test_tobacco_hsn_keeps_28_post_switch():
    assert 2800 in tr.allowed_rates_bps("2026-10-18", hsn="24011020")
    assert 2800 in tr.allowed_rates_bps("2026-10-18", hsn="21069020")
    assert 2800 not in tr.allowed_rates_bps("2026-10-18", hsn="7308")


def test_tax_head_rule():
    assert tr.expected_tax_head("24", "32") == "IGST"
    assert tr.expected_tax_head("24", "24") == "CGST_SGST"
    assert tr.expected_tax_head("32", "32") == "CGST_SGST"
