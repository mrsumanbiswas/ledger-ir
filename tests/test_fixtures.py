"""Fixture self-checks (no solver needed — these pass before Phase 2 code exists):
sample_data/ must match SPEC.md §3 exactly and validate against the §2 schemas."""
from __future__ import annotations

import csv
import io

from PIL import Image

from tests.reference import inr_to_paise, is_valid_gstin_ref

SUPPLIER = "24ABCDE1234F1Z6"
BUYER = "32FGHIJ5678K1ZA"
POST_SWITCH_RATES = {0, 500, 1800, 4000, 300, 25}


def test_all_six_schemas_present_in_spec(spec_schemas):
    assert set(spec_schemas) == {"ocr_tokens", "invoice_ast", "gstr2b_record",
                                 "bank_entry", "match_edge", "repair_proof"}


def test_gstr2b_records_schema_valid_and_golden(gstr2b, validate):
    assert gstr2b["period"] == "2026-10"
    assert gstr2b["buyer_gstin"] == BUYER
    recs = {r["inv_no"]: r for r in gstr2b["records"]}
    assert set(recs) == {"INV-881", "INV-882", "INV-883", "CN-12"}
    for r in gstr2b["records"]:
        validate("gstr2b_record", r)
        assert r["supplier_gstin"] == SUPPLIER
        assert r["rate_bps"] in POST_SWITCH_RATES
        assert r["inv_date"] >= "2025-09-22"
        assert r["total_paise"] == (r["taxable_value_paise"] + r["cgst_paise"]
                                    + r["sgst_paise"] + r["igst_paise"]
                                    + r["cess_paise"])
    assert recs["INV-881"]["total_paise"] == 5_900_000
    assert recs["INV-883"]["total_paise"] == 3_540_000
    assert recs["CN-12"]["total_paise"] == 118_000
    assert recs["CN-12"]["doc_type"] == "CDN"
    assert recs["CN-12"]["original_inv_no"] == "INV-881"
    # the golden NEFT equation
    assert (recs["INV-881"]["total_paise"] + recs["INV-883"]["total_paise"]
            - recs["CN-12"]["total_paise"]) == 9_322_000
    # INV-882 mirrors the supplier's wrong intrastate split on an interstate supply
    r882 = recs["INV-882"]
    assert (r882["cgst_paise"], r882["sgst_paise"], r882["igst_paise"]) == \
        (360_000, 360_000, 0)
    assert r882["total_paise"] == 4_720_000


def test_fixture_gstins_synthetic_but_checksum_valid(gstr2b):
    assert is_valid_gstin_ref(SUPPLIER) and SUPPLIER[:2] == "24"
    assert is_valid_gstin_ref(BUYER) and BUYER[:2] == "32"
    assert SUPPLIER[:2] != BUYER[:2], "golden sample is interstate"


def test_bank_csv_golden(bank_csv_text):
    rows = list(csv.DictReader(io.StringIO(bank_csv_text)))
    assert len(rows) == 4
    neft = next(r for r in rows if r["txn_id"] == "UTIB2026102803")
    assert inr_to_paise(neft["debit_inr"]) == 9_322_000
    assert neft["mode"] == "NEFT"
    assert "SHREE GANESH STEELS" in neft["narration"]
    # running balance is arithmetically consistent
    prev = None
    for r in rows:
        bal = inr_to_paise(r["balance_inr"])
        delta = (inr_to_paise(r["credit_inr"]) if r["credit_inr"]
                 else -inr_to_paise(r["debit_inr"]))
        if prev is not None:
            assert bal == prev + delta
        prev = bal


def test_token_file_and_image_consistent(invoice_tokens, sample_dir, validate):
    validate("ocr_tokens", invoice_tokens)
    img = Image.open(sample_dir / invoice_tokens["image"])
    assert (img.width, img.height) == (invoice_tokens["width"],
                                       invoice_tokens["height"])
    for t in invoice_tokens["tokens"]:
        assert t["x"] >= 0 and t["x"] + t["w"] <= img.width
        assert t["y"] >= 0 and t["y"] + t["h"] <= img.height
    # exactly one low-confidence token: the misread blurred digit
    low = [t for t in invoice_tokens["tokens"] if t["conf"] < 0.9]
    assert len(low) == 1
    assert low[0]["text"] == "8,000.00"
    assert low[0]["conf"] == 0.62
    texts = [t["text"] for t in invoice_tokens["tokens"]]
    for expected in ("INV-882", SUPPLIER, BUYER, "40,000.00", "47,200.00"):
        assert any(expected in s for s in texts), expected


def test_api_handler_seed_data_in_sync(repo_root, sample_dir):
    """/demo-seed ships bundled copies of the fixtures; they must be
    byte-identical to sample_data/ (regenerate via scripts/generate_fixtures.py)."""
    seed_dir = repo_root / "functions" / "api_handler" / "seed_data"
    for name in ("invoice_882_flawed.png", "invoice_882_tokens.json",
                 "gstr2b_oct2026.json", "bank_oct2026.csv"):
        assert (seed_dir / name).read_bytes() == (sample_dir / name).read_bytes(), \
            f"{name}: seed_data out of sync with sample_data"
