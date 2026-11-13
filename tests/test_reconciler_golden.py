"""Golden lump-sum reconciliation (SPEC.md §1.6, §3.3):
NEFT ₹93,220 = INV-881 (₹59,000) + INV-883 (₹35,400) − CN-12 (₹1,180), residual 0.
"""
from __future__ import annotations

import json

import pytest

from functions.graph_reconciler.core.reconciler import parse_bank_csv, reconcile
from tests.reference import edge_id_ref

NEFT_TXN = "UTIB2026102803"


@pytest.fixture()
def bank_entries(bank_csv_text):
    return parse_bank_csv(bank_csv_text)


def test_parse_bank_csv(bank_entries, validate):
    assert len(bank_entries) == 4
    for e in bank_entries:
        validate("bank_entry", e)
    neft = next(e for e in bank_entries if e["txn_id"] == NEFT_TXN)
    assert neft["amount_paise"] == 9_322_000
    assert neft["direction"] == "DEBIT"
    assert neft["mode"] == "NEFT"
    assert neft["value_date"] == "2026-10-28"
    assert "SHREE GANESH STEELS" in neft["narration"]


def test_golden_lump_sum_exact_match(gstr2b, bank_entries, validate):
    edges = reconcile(gstr2b["records"], bank_entries)
    assert len(edges) == 1, "exactly one settlement; decoy entries must not match"
    edge = edges[0]
    validate("match_edge", edge)
    assert edge["bank_txn_id"] == NEFT_TXN
    assert edge["residual_paise"] == 0
    assert edge["matched_amount_paise"] == 9_322_000
    assert edge["method"] == "EXACT_SUBSET"
    docs = {(d["doc_type"], d["doc_no"], d["sign"], d["amount_paise"])
            for d in edge["docs"]}
    assert docs == {
        ("INV", "INV-881", 1, 5_900_000),
        ("INV", "INV-883", 1, 3_540_000),
        ("CDN", "CN-12", -1, 118_000),
    }
    assert sum(d["sign"] * d["amount_paise"] for d in edge["docs"]) == 9_322_000
    assert edge["edge_id"] == edge_id_ref(
        NEFT_TXN, [("CN-12", -1), ("INV-881", 1), ("INV-883", 1)])
    assert edge["score"]["counterparty_similarity_bps"] > 5000
    assert 0 <= edge["score"]["max_day_distance"] <= 90


def test_unpaid_invoice_882_stays_unmatched(gstr2b, bank_entries):
    edges = reconcile(gstr2b["records"], bank_entries)
    matched_docs = {d["doc_no"] for e in edges for d in e["docs"]}
    assert "INV-882" not in matched_docs


def test_reconciliation_is_deterministic(gstr2b, bank_entries):
    a = reconcile(gstr2b["records"], bank_entries)
    b = reconcile(list(reversed(gstr2b["records"])), list(reversed(bank_entries)))
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
