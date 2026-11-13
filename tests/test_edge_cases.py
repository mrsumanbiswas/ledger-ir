"""Robustness tests beyond the golden sample: these guard the behaviours a real
deployment hits first (big candidate pools, misread GSTINs, malformed CSVs,
money-parsing strictness, Merkle misuse)."""
from __future__ import annotations

import pytest

from functions.ast_compiler.core.compiler import compile_invoice
from functions.ast_compiler.core.money import normalize_date, parse_inr, pct_to_bps
from functions.graph_reconciler.core.merkle import merkle_root
from functions.graph_reconciler.core.reconciler import parse_bank_csv, reconcile
from tests.test_ast_compiler_golden import replace_token


# --------------------------------------------------------------------------- #
# money parsing is strict: anything else must raise, never guess
# --------------------------------------------------------------------------- #

def test_parse_inr_accepts_indian_and_plain_formats():
    assert parse_inr("93,220.00") == 9_322_000
    assert parse_inr("1,25,000.00") == 12_500_000
    assert parse_inr("2500") == 250_000
    assert parse_inr("₹ 47,200.00") == 4_720_000


@pytest.mark.parametrize("bad", ["", "8,0O0.00", "B,000.00", "1.5", "12.345",
                                 "1,2,3.00", "-500.00", "47 200.00"])
def test_parse_inr_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_inr(bad)


def test_pct_and_date_helpers():
    assert pct_to_bps("9") == 900
    assert pct_to_bps("0.25%") == 25
    assert pct_to_bps("40") == 4000
    assert normalize_date("18-10-2026") == "2026-10-18"
    assert normalize_date("2026-10-18") == "2026-10-18"
    with pytest.raises(ValueError):
        normalize_date("31-02-2026")  # nonexistent date


# --------------------------------------------------------------------------- #
# compiler: GSTIN checksum repair, and combined repairs
# --------------------------------------------------------------------------- #

def test_low_conf_misread_gstin_repaired_via_checksum(invoice_tokens, validate):
    tf = replace_token(invoice_tokens, "GSTIN: 24ABCDE1234F1Z6",
                       "GSTIN: 24A8CDE1234F1Z6", conf=0.65)  # B misread as 8
    ast = compile_invoice(tf)
    assert ast["status"] == "COMPILED"
    assert ast["supplier"]["gstin"] == "24ABCDE1234F1Z6"
    gstin_repairs = [r for r in ast["repairs"] if r["field"] == "supplier.gstin"]
    assert len(gstin_repairs) == 1
    proof = gstin_repairs[0]
    validate("repair_proof", proof)
    assert proof["original"] == "24A8CDE1234F1Z6"
    assert proof["repaired"] == "24ABCDE1234F1Z6"
    assert sum(c["satisfied"] for c in proof["candidates_tried"]) == 1
    # the numeric blurred-digit repair still happens alongside: two proofs total
    assert len(ast["repairs"]) == 2
    # and the interstate tax-head supplier error is unaffected
    assert [e["code"] for e in ast["errors"]] == ["TAX_HEAD_MISMATCH"]


def test_high_conf_invalid_gstin_is_supplier_error(invoice_tokens):
    tf = replace_token(invoice_tokens, "GSTIN: 24ABCDE1234F1Z6",
                       "GSTIN: 24ABCDE1234F1Z9", conf=0.97)  # bad check char
    ast = compile_invoice(tf)
    assert any(e["code"] == "GSTIN_CHECKSUM_FAIL" for e in ast["errors"])
    assert all(r["field"] != "supplier.gstin" for r in ast["repairs"])


# --------------------------------------------------------------------------- #
# reconciler: meet-in-the-middle path and CSV strictness
# --------------------------------------------------------------------------- #

def _doc(no: str, total: int, date: str = "2026-10-10") -> dict:
    return {"doc_type": "INV", "inv_no": no, "inv_date": date,
            "supplier_name": "SHREE GANESH STEELS", "total_paise": total}


def test_meet_in_the_middle_over_20_candidates():
    # powers of two make every subset sum unique, so the answer is provable
    docs = [_doc(f"INV-9{i:02d}", 2 ** i) for i in range(22)]
    target = 2 ** 3 + 2 ** 10 + 2 ** 20
    entry = {"txn_id": "TXN-MITM", "value_date": "2026-10-20",
             "narration": "NEFT/DR/SHREE GANESH STEELS/BULK", "mode": "NEFT",
             "direction": "DEBIT", "amount_paise": target}
    edges = reconcile(docs, [entry])
    assert len(edges) == 1
    edge = edges[0]
    assert edge["method"] == "MEET_IN_MIDDLE"
    assert edge["residual_paise"] == 0
    assert {d["doc_no"] for d in edge["docs"]} == {"INV-903", "INV-910", "INV-920"}


def test_each_document_settles_at_most_once():
    docs = [_doc("INV-A", 1_000_00), _doc("INV-B", 2_000_00)]
    pay = {"txn_id": "T1", "value_date": "2026-10-20",
           "narration": "NEFT SHREE GANESH STEELS", "mode": "NEFT",
           "direction": "DEBIT", "amount_paise": 1_000_00}
    again = dict(pay, txn_id="T2", value_date="2026-10-21")
    edges = reconcile(docs, [pay, again])
    assert [e["bank_txn_id"] for e in edges] == ["T1"]  # T2 finds INV-A gone


def test_bank_csv_rejects_missing_columns_and_double_amounts():
    with pytest.raises(ValueError, match="missing columns"):
        parse_bank_csv("txn_id,value_date\nX,2026-10-01\n")
    both = ("txn_id,value_date,narration,mode,debit_inr,credit_inr,balance_inr\n"
            "X,2026-10-01,FOO,NEFT,10.00,20.00,30.00\n")
    with pytest.raises(ValueError, match="exactly one"):
        parse_bank_csv(both)


# --------------------------------------------------------------------------- #
# merkle: misuse guards
# --------------------------------------------------------------------------- #

def test_merkle_rejects_empty_and_duplicate_sks():
    with pytest.raises(ValueError, match="no records"):
        merkle_root([])
    with pytest.raises(ValueError, match="duplicate"):
        merkle_root([("DOC#INV#1", {"a": 1}), ("DOC#INV#1", {"a": 2})])
