"""Canonical JSON + SHA-256 Merkle dossier (SPEC.md §1.8).

Pure Python, zero AWS imports. Determinism contract: leaves are
sha256(canonical_json(record)) sorted by the record's DynamoDB SK; an odd
node at any level is promoted unchanged (never hashed with itself); the root
depends only on the record *contents and SKs*, never on input order.
"""
from __future__ import annotations

import hashlib
import json


def canonical_json(obj) -> bytes:
    """The one serialization every hash in LedgerIR is computed over."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def merkle_root(records: list[tuple[str, dict]]) -> str:
    """Hex SHA-256 Merkle root over ``records`` = [(sk, record), ...]."""
    if not records:
        raise ValueError("merkle_root: no records")
    sks = [sk for sk, _ in records]
    if len(set(sks)) != len(sks):
        dupes = sorted({sk for sk in sks if sks.count(sk) > 1})
        raise ValueError(f"merkle_root: duplicate SKs {dupes}")
    level = [hashlib.sha256(canonical_json(record)).digest()
             for _, record in sorted(records, key=lambda kv: kv[0])]
    while len(level) > 1:
        nxt = [hashlib.sha256(level[i] + level[i + 1]).digest()
               for i in range(0, len(level) - 1, 2)]
        if len(level) % 2:
            nxt.append(level[-1])
        level = nxt
    return level[0].hex()
