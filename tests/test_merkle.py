"""Merkle dossier determinism (SPEC.md §1.8): canonical JSON, SK-sorted leaves,
odd-node promotion, identical root across runs and input orderings."""
from __future__ import annotations

import hashlib

from functions.graph_reconciler.core.merkle import canonical_json, merkle_root
from tests.reference import canonical_json_ref, merkle_root_ref


def test_canonical_json_exact_bytes():
    obj = {"b": 1, "a": [1, 2], "c": "₹"}
    assert canonical_json(obj) == '{"a":[1,2],"b":1,"c":"₹"}'.encode()
    assert canonical_json(obj) == canonical_json_ref(obj)


def test_single_leaf_root():
    rec = {"x": 1}
    expected = hashlib.sha256(canonical_json_ref(rec)).hexdigest()
    assert merkle_root([("DOC#INV#1", rec)]) == expected


def test_two_and_three_leaves_against_reference():
    records = [
        ("DOC#BANK#UTIB2026102803", {"amount_paise": 9_322_000}),
        ("DOC#INV#INV-881", {"total_paise": 5_900_000}),
        ("DOC#INV#INV-883", {"total_paise": 3_540_000}),
    ]
    assert merkle_root(records[:2]) == merkle_root_ref(records[:2])
    assert merkle_root(records) == merkle_root_ref(records)  # odd-node promotion


def test_root_independent_of_input_order(gstr2b):
    records = [(f"DOC#GSTR2B#{r['inv_no']}", r) for r in gstr2b["records"]]
    assert merkle_root(records) == merkle_root(list(reversed(records)))


def test_same_input_same_root_twice(gstr2b):
    records = [(f"DOC#GSTR2B#{r['inv_no']}", r) for r in gstr2b["records"]]
    assert merkle_root(records) == merkle_root(records)
    assert merkle_root(records) == merkle_root_ref(records)


def test_tampering_changes_root(gstr2b):
    records = [(f"DOC#GSTR2B#{r['inv_no']}", dict(r)) for r in gstr2b["records"]]
    root = merkle_root(records)
    records[0][1]["taxable_value_paise"] += 1
    assert merkle_root(records) != root
