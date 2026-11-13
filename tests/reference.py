"""Independent reference oracles for tests (deliberately duplicated from
SPEC.md, NOT imported from functions/ — so fixture validity and solver
correctness are checked against a second implementation)."""
from __future__ import annotations

import hashlib
import json
import re

C36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def gstin_check_ref(g: str) -> str:
    s = 0
    for i, ch in enumerate(g[:14]):
        p = C36.index(ch) * (1 if i % 2 == 0 else 2)
        s += p // 36 + p % 36
    return C36[(36 - s % 36) % 36]


def is_valid_gstin_ref(g: str) -> bool:
    return bool(GSTIN_RE.match(g)) and gstin_check_ref(g) == g[14]


def inr_to_paise(s: str) -> int:
    """'93,220.00' / '93220.00' -> 9322000. Integer math only."""
    s = s.replace(",", "").strip()
    rupees, paise = s.split(".")
    assert len(paise) == 2, s
    return int(rupees) * 100 + int(paise)


def canonical_json_ref(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def merkle_root_ref(records: list[tuple[str, dict]]) -> str:
    """SPEC.md §1.8: leaves sorted by SK, odd node promoted unchanged."""
    level = [hashlib.sha256(canonical_json_ref(rec)).digest()
             for _, rec in sorted(records, key=lambda kv: kv[0])]
    assert level, "no records"
    while len(level) > 1:
        nxt = [hashlib.sha256(level[i] + level[i + 1]).digest()
               for i in range(0, len(level) - 1, 2)]
        if len(level) % 2:
            nxt.append(level[-1])
        level = nxt
    return level[0].hex()


def edge_id_ref(bank_txn_id: str, docs: list[tuple[str, int]]) -> str:
    """SPEC.md §1.6 rule 5: sha256 over canonical {bank_txn_id, docs sorted by doc_no}."""
    preimage = {"bank_txn_id": bank_txn_id,
                "docs": [[no, sign] for no, sign in sorted(docs)]}
    return hashlib.sha256(canonical_json_ref(preimage)).hexdigest()[:16]
