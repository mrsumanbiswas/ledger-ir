"""GST IMS triage decision table (SPEC.md §1.7). Pure Python, zero AWS imports.

First match wins, top-down: REJECT > HOLD_GST > AUTO_ACCEPT > AUTO_PENDING.
A fault always outranks acceptance; acceptance requires positive evidence
(a clean compiled AST, or settlement proven by a match edge); the safe
default is AUTO_PENDING.

The GSTIN checksum is duplicated from ast_compiler deliberately: each Lambda
bundles only its own ``core/``, and tests cross-check both implementations.
"""
from __future__ import annotations

import re

TOLERANCE_PAISE = 100

_C36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def _gstin_valid(gstin: str) -> bool:
    if not isinstance(gstin, str) or not _GSTIN_RE.match(gstin):
        return False
    s = 0
    for i, ch in enumerate(gstin[:14]):
        p = _C36.index(ch) * (1 if i % 2 == 0 else 2)
        s += p // 36 + p % 36
    return _C36[(36 - s % 36) % 36] == gstin[14]


def _tax_total(tax_like: dict, prefix: str = "") -> int:
    return sum(tax_like.get(f"{prefix}{part}_paise", 0)
               for part in ("cgst", "sgst", "igst", "cess"))


def _record_head(record: dict) -> str | None:
    if record.get("cgst_paise", 0) + record.get("sgst_paise", 0) > 0:
        return "CGST_SGST"
    if record.get("igst_paise", 0) > 0:
        return "IGST"
    return None


def _is_settled(doc_no: str, edges: list[dict]) -> bool:
    return any(d["doc_no"] == doc_no
               for e in edges if abs(e["residual_paise"]) <= TOLERANCE_PAISE
               for d in e["docs"])


def triage(ast: dict | None, gstr2b_record: dict | None, edges: list[dict],
           *, duplicate: bool = False) -> dict:
    """Decide the IMS action for one document (SPEC.md §1.7).

    ``duplicate`` is supplied by the caller, which sees the whole monthly
    batch and can detect a repeated inv_no for the same supplier+period.
    """
    def result(action: str, rule: int, holdback: int = 0) -> dict:
        return {"action": action, "rule": rule, "holdback_paise": holdback}

    # Rule 1 — REJECT: structurally bad or duplicated supplier filing
    if duplicate or (gstr2b_record is not None
                     and not _gstin_valid(gstr2b_record["supplier_gstin"])):
        return result("REJECT", 1)

    # Rule 2 — HOLD_GST: the supplier's document is legally wrong
    if ast is not None:
        if any(e["class"] == "SUPPLIER_ERROR" for e in ast.get("errors", [])):
            return result("HOLD_GST", 2, holdback=_tax_total(ast["tax"]))
        rec_head = _record_head(gstr2b_record) if gstr2b_record else None
        if rec_head is not None and ast["tax"]["head"] != rec_head:
            return result("HOLD_GST", 2, holdback=_tax_total(ast["tax"]))

    # Rule 3 — AUTO_ACCEPT: clean and positively evidenced
    if gstr2b_record is not None:
        if ast is not None:
            consistent = (
                ast.get("status") == "COMPILED"
                and abs(ast["taxable_value_paise"]
                        - gstr2b_record["taxable_value_paise"]) <= TOLERANCE_PAISE
                and abs(_tax_total(ast["tax"])
                        - _tax_total(gstr2b_record)) <= TOLERANCE_PAISE)
            if consistent:
                return result("AUTO_ACCEPT", 3)
        elif _is_settled(gstr2b_record["inv_no"], edges):
            return result("AUTO_ACCEPT", 3)

    # Rule 4 — AUTO_PENDING: everything else waits for a human or more data
    return result("AUTO_PENDING", 4)
