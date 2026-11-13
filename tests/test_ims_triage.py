"""IMS triage decision table (SPEC.md §1.7, §3.4): first match wins,
REJECT > HOLD_GST > AUTO_ACCEPT > AUTO_PENDING."""
from __future__ import annotations

import copy


from functions.graph_reconciler.core.ims import triage
from tests.reference import edge_id_ref

AST_882 = {
    "doc_type": "INV", "inv_no": "INV-882", "inv_date": "2026-10-18",
    "supplier": {"name": "SHREE GANESH STEELS", "gstin": "24ABCDE1234F1Z6",
                 "state_code": "24"},
    "buyer": {"name": "MALABAR FABRICATORS", "gstin": "32FGHIJ5678K1ZA",
              "state_code": "32"},
    "place_of_supply": "32",
    "line_items": [
        {"description": "MS Steel Brackets", "hsn": "7308", "qty": 10,
         "unit_price_paise": 250_000, "discount_paise": 0,
         "line_total_paise": 2_500_000},
        {"description": "Mounting Rails", "hsn": "7308", "qty": 5,
         "unit_price_paise": 300_000, "discount_paise": 0,
         "line_total_paise": 1_500_000},
    ],
    "taxable_value_paise": 4_000_000,
    "tax": {"head": "CGST_SGST", "rate_bps": 1800, "cgst_paise": 360_000,
            "sgst_paise": 360_000, "igst_paise": 0, "cess_paise": 0},
    "total_paise": 4_720_000,
    "errors": [{"class": "SUPPLIER_ERROR", "code": "TAX_HEAD_MISMATCH",
                "field": "tax.head",
                "detail": "CGST+SGST on interstate supply 24->32; IGST expected"}],
    "repairs": [],
    "status": "COMPILED",
}

GOLDEN_EDGE = {
    "edge_id": edge_id_ref("UTIB2026102803",
                           [("CN-12", -1), ("INV-881", 1), ("INV-883", 1)]),
    "bank_txn_id": "UTIB2026102803",
    "docs": [
        {"doc_type": "CDN", "doc_no": "CN-12", "sign": -1, "amount_paise": 118_000},
        {"doc_type": "INV", "doc_no": "INV-881", "sign": 1, "amount_paise": 5_900_000},
        {"doc_type": "INV", "doc_no": "INV-883", "sign": 1, "amount_paise": 3_540_000},
    ],
    "matched_amount_paise": 9_322_000,
    "residual_paise": 0,
    "method": "EXACT_SUBSET",
    "score": {"counterparty_similarity": 1.0, "max_day_distance": 23},
}


def rec_by_no(gstr2b: dict, no: str) -> dict:
    return next(r for r in gstr2b["records"] if r["inv_no"] == no)


def test_golden_hold_gst_with_18pct_holdback(gstr2b):
    result = triage(AST_882, rec_by_no(gstr2b, "INV-882"), [])
    assert result["action"] == "HOLD_GST"
    assert result["holdback_paise"] == 720_000  # the 18% GST portion of ₹47,200
    assert result["rule"] == 2


def test_settled_clean_records_auto_accept(gstr2b):
    for no in ("INV-881", "INV-883", "CN-12"):
        result = triage(None, rec_by_no(gstr2b, no), [GOLDEN_EDGE])
        assert result["action"] == "AUTO_ACCEPT", no
        assert result["holdback_paise"] == 0
        assert result["rule"] == 3


def test_unsettled_record_without_ast_is_pending(gstr2b):
    result = triage(None, rec_by_no(gstr2b, "INV-881"), [])
    assert result["action"] == "AUTO_PENDING"
    assert result["rule"] == 4


def test_needs_review_ast_is_pending(gstr2b):
    ast = copy.deepcopy(AST_882)
    ast["status"] = "NEEDS_REVIEW"
    ast["errors"] = []
    result = triage(ast, rec_by_no(gstr2b, "INV-882"), [])
    assert result["action"] == "AUTO_PENDING"


def test_checksum_failure_rejects_even_with_other_faults(gstr2b):
    bad = copy.deepcopy(rec_by_no(gstr2b, "INV-882"))
    bad["supplier_gstin"] = "24ABCDE1234F1Z7"  # corrupted check char
    result = triage(AST_882, bad, [GOLDEN_EDGE])
    assert result["action"] == "REJECT"
    assert result["rule"] == 1
    assert result["holdback_paise"] == 0


def test_amount_mismatch_beyond_tolerance_is_pending(gstr2b):
    rec = copy.deepcopy(rec_by_no(gstr2b, "INV-882"))
    rec["taxable_value_paise"] += 101  # just past the 100-paise tolerance
    ast = copy.deepcopy(AST_882)
    ast["errors"] = []  # isolate the amount-mismatch condition from rule 2
    result = triage(ast, rec, [])
    assert result["action"] == "AUTO_PENDING"
