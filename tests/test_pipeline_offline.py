"""Offline end-to-end: chain every pure solver exactly as the Step Functions
pipeline will (SPEC.md §3.4), with zero AWS. This is the Phase-2 'done when'
gate: the golden sample passes and two full runs produce the identical
Merkle root.
"""
from __future__ import annotations

import json

from functions.ast_compiler.core.compiler import compile_invoice
from functions.graph_reconciler.core.ims import triage
from functions.graph_reconciler.core.merkle import merkle_root
from functions.graph_reconciler.core.reconciler import parse_bank_csv, reconcile


def run_pipeline(invoice_tokens: dict, gstr2b: dict, bank_csv_text: str) -> dict:
    """What the state machine does, minus S3/DynamoDB: returns every record
    keyed by its would-be DynamoDB SK, plus the Merkle root over all of them."""
    ast = compile_invoice(invoice_tokens)
    bank = parse_bank_csv(bank_csv_text)
    edges = reconcile(gstr2b["records"], bank)

    asts = {ast["inv_no"]: ast}
    records: dict[str, dict] = {f"DOC#INV#{ast['inv_no']}": ast}
    for rec in gstr2b["records"]:
        records[f"DOC#GSTR2B#{rec['inv_no']}"] = rec
    for entry in bank:
        records[f"DOC#BANK#{entry['txn_id']}"] = entry
    for edge in edges:
        records[f"MATCH#{edge['edge_id']}"] = edge

    ims_results = {rec["inv_no"]: triage(asts.get(rec["inv_no"]), rec, edges)
                   for rec in gstr2b["records"]}
    records["SUMMARY#MONTHLY"] = {
        "period": gstr2b["period"],
        "ims": ims_results,
        "total_holdback_paise": sum(r["holdback_paise"]
                                    for r in ims_results.values()),
    }
    return {"records": records, "ims": ims_results,
            "merkle_root": merkle_root(list(records.items()))}


def test_golden_sample_end_to_end(invoice_tokens, gstr2b, bank_csv_text):
    out = run_pipeline(invoice_tokens, gstr2b, bank_csv_text)
    ims = out["ims"]
    assert ims["INV-882"] == {"action": "HOLD_GST", "rule": 2,
                              "holdback_paise": 720_000}
    for no in ("INV-881", "INV-883", "CN-12"):
        assert ims[no]["action"] == "AUTO_ACCEPT", no
    assert out["records"]["SUMMARY#MONTHLY"]["total_holdback_paise"] == 720_000


def test_two_full_runs_identical_merkle_root(invoice_tokens, gstr2b,
                                             bank_csv_text):
    first = run_pipeline(invoice_tokens, gstr2b, bank_csv_text)
    second = run_pipeline(
        json.loads(json.dumps(invoice_tokens)),  # fresh objects, same content
        json.loads(json.dumps(gstr2b)),
        bank_csv_text)
    assert first["merkle_root"] == second["merkle_root"]
    assert len(first["merkle_root"]) == 64  # full SHA-256 hex
