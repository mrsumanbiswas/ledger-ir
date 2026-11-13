"""Golden-sample compiler contract for invoice_882_flawed (SPEC.md §1.4–1.5, §3.1).

Asserts the golden sample exactly: one OCR_REPAIRED (the blurred digit,
with a full uniqueness proof) + one SUPPLIER_ERROR (CGST+SGST on an interstate
bill), and the repair-uniqueness rule's NEEDS_REVIEW branches.
"""
from __future__ import annotations

import copy

import pytest

from functions.ast_compiler.core.compiler import compile_invoice

SUPPLIER = "24ABCDE1234F1Z6"
BUYER = "32FGHIJ5678K1ZA"


def replace_token(token_file: dict, old: str, new: str,
                  conf: float | None = None, count: int = 1) -> dict:
    tf = copy.deepcopy(token_file)
    hits = [t for t in tf["tokens"] if t["text"] == old]
    assert len(hits) == count, f"token {old!r}: expected {count} hits, got {len(hits)}"
    for t in hits:
        t["text"] = new
        if conf is not None:
            t["conf"] = conf
    return tf


@pytest.fixture()
def golden_ast(invoice_tokens):
    return compile_invoice(invoice_tokens)


def test_exactly_one_ocr_repair_with_unique_proof(golden_ast, validate):
    repairs = golden_ast["repairs"]
    assert len(repairs) == 1
    proof = repairs[0]
    validate("repair_proof", proof)
    assert proof["class"] == "OCR_REPAIRED"
    assert proof["field"] == "line_items[1].unit_price_paise"
    assert proof["original"] == "8,000.00"
    assert proof["repaired"] == "3,000.00"
    assert proof["token_conf"] < 0.9
    survivors = [c for c in proof["candidates_tried"] if c["satisfied"]]
    assert len(survivors) == 1 and survivors[0]["candidate"] == "3,000.00"
    # the as-read value and the non-numeric confusion (8->B) were tried and rejected
    tried = {c["candidate"] for c in proof["candidates_tried"]}
    assert "8,000.00" in tried
    assert any("B" in c for c in tried)


def test_exactly_one_supplier_error_tax_head(golden_ast):
    errors = golden_ast["errors"]
    assert len(errors) == 1
    err = errors[0]
    assert err["class"] == "SUPPLIER_ERROR"
    assert err["code"] == "TAX_HEAD_MISMATCH"
    # the supplier's legal document is never "repaired": printed head survives
    assert golden_ast["tax"]["head"] == "CGST_SGST"
    assert golden_ast["tax"]["cgst_paise"] == 360_000
    assert golden_ast["tax"]["sgst_paise"] == 360_000
    assert golden_ast["tax"]["igst_paise"] == 0


def test_invariants_hold_after_repair(golden_ast):
    lines = golden_ast["line_items"]
    assert [(ln["qty"], ln["unit_price_paise"], ln["line_total_paise"])
            for ln in lines] == [
        (10, 250_000, 2_500_000),
        (5, 300_000, 1_500_000),  # repaired: 8,000.00 -> 3,000.00
    ]
    for ln in lines:
        assert ln["qty"] * ln["unit_price_paise"] - ln["discount_paise"] \
            == ln["line_total_paise"]
    assert golden_ast["taxable_value_paise"] == 4_000_000
    assert golden_ast["total_paise"] == 4_720_000
    assert golden_ast["tax"]["rate_bps"] == 1800


def test_ast_fields_and_schema(golden_ast, validate):
    validate("invoice_ast", golden_ast)
    assert golden_ast["status"] == "COMPILED"
    assert golden_ast["doc_type"] == "INV"
    assert golden_ast["inv_no"] == "INV-882"
    assert golden_ast["inv_date"] == "2026-10-18"
    assert golden_ast["supplier"]["gstin"] == SUPPLIER
    assert golden_ast["supplier"]["state_code"] == "24"
    assert golden_ast["buyer"]["gstin"] == BUYER
    assert golden_ast["buyer"]["state_code"] == "32"
    assert golden_ast["place_of_supply"] == "32"


def test_money_is_integer_paise_everywhere(golden_ast):
    def walk(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "field_provenance":
                    continue  # maps field *names* to token indices, not money
                if k.endswith("_paise"):
                    assert type(v) is int, f"{path}.{k} = {v!r} is not int paise"
                walk(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                walk(v, f"{path}[{i}]")

    walk(golden_ast)


def test_zero_repair_candidates_needs_review(invoice_tokens):
    # '9' has no confusion partner; every 0->O variant is non-numeric: zero survivors
    tf = replace_token(invoice_tokens, "8,000.00", "9,000.00", conf=0.62)
    ast = compile_invoice(tf)
    assert ast["status"] == "NEEDS_REVIEW"
    assert ast["repairs"] == []


def test_ambiguous_repair_candidates_needs_review(invoice_tokens):
    # Within the 100-paise tolerance several variants of the low-conf rate token
    # survive (3,100.31 / 3,100.37 / 3,100.81 vs line total 3,100.00): ambiguity.
    tf = invoice_tokens
    tf = replace_token(tf, "5", "1")                                # line 2 qty
    tf = replace_token(tf, "8,000.00", "8,100.31", conf=0.62)       # line 2 rate
    tf = replace_token(tf, "15,000.00", "3,100.00")                 # line 2 amount
    tf = replace_token(tf, "40,000.00", "28,100.00")                # taxable
    tf = replace_token(tf, "3,600.00", "2,529.00", count=2)         # CGST + SGST
    tf = replace_token(tf, "47,200.00", "33,158.00")                # total
    ast = compile_invoice(tf)
    assert ast["status"] == "NEEDS_REVIEW"
    assert ast["repairs"] == []


def test_high_confidence_mismatch_is_supplier_arithmetic_not_repair(invoice_tokens):
    # same broken number, but read confidently (>= 0.9): not repair-eligible
    tf = replace_token(invoice_tokens, "8,000.00", "8,000.00", conf=0.95)
    ast = compile_invoice(tf)
    assert ast["repairs"] == []
    assert any(e["code"] == "ARITHMETIC_MISMATCH" for e in ast["errors"])
