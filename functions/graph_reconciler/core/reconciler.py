"""Bank CSV parsing and signed subset-sum reconciliation (SPEC.md §1.6, §2.4–2.5).

Pure Python, zero AWS imports. Deliberately self-contained (its own tiny money
parser) because each Lambda bundles only its own ``core/``.

Determinism contract (§1.6): bank entries are processed in (value_date, txn_id)
order, candidate documents are considered in doc_no order, each document
settles at most one bank entry, and ties break by (|residual|, subset size,
lexicographic doc_nos). Same input always produces the same edges.
"""
from __future__ import annotations

import bisect
import csv
import datetime
import hashlib
import io
import json
import re
from itertools import combinations

TOLERANCE_PAISE = 100
MAX_SUBSET_SIZE = 5
MITM_THRESHOLD = 20          # > 20 candidates -> meet-in-the-middle
SIMILARITY_THRESHOLD_BPS = 6000
WINDOW_DAYS = 90

_REQUIRED_COLUMNS = ["txn_id", "value_date", "narration", "mode",
                     "debit_inr", "credit_inr", "balance_inr"]
_MODES = {"NEFT", "UPI", "IMPS", "RTGS", "ACH"}
_INR_RE = re.compile(r"^(?:\d{1,3}(?:,\d{2,3})*|\d+)(?:\.\d{2})?$")


def _parse_inr(text: str) -> int:
    t = text.strip()
    if not _INR_RE.match(t):
        raise ValueError(f"not an INR amount: {text!r}")
    t = t.replace(",", "")
    if "." in t:
        rupees, paise = t.split(".")
        return int(rupees) * 100 + int(paise)
    return int(t) * 100


def parse_bank_csv(text: str) -> list[dict]:
    """Parse a bank statement CSV (SPEC.md §2.4 header) into BankEntry dicts."""
    reader = csv.DictReader(io.StringIO(text))
    missing = set(_REQUIRED_COLUMNS) - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"bank CSV missing columns: {sorted(missing)}")
    entries: list[dict] = []
    for lineno, row in enumerate(reader, start=2):
        debit, credit = row["debit_inr"].strip(), row["credit_inr"].strip()
        if bool(debit) == bool(credit):
            raise ValueError(f"bank CSV line {lineno}: exactly one of "
                             f"debit_inr/credit_inr must be set")
        entry = {
            "txn_id": row["txn_id"].strip(),
            "value_date": row["value_date"].strip(),
            "narration": row["narration"].strip(),
            "mode": row["mode"].strip().upper()
                    if row["mode"].strip().upper() in _MODES else "OTHER",
            "direction": "DEBIT" if debit else "CREDIT",
            "amount_paise": _parse_inr(debit or credit),
        }
        if row["balance_inr"].strip():
            entry["balance_paise"] = _parse_inr(row["balance_inr"])
        if not entry["txn_id"]:
            raise ValueError(f"bank CSV line {lineno}: empty txn_id")
        datetime.date.fromisoformat(entry["value_date"])  # validate
        entries.append(entry)
    return entries


def counterparty_similarity_bps(narration: str, supplier_name: str) -> int:
    """Share of the supplier's name words present in the narration, in integer
    basis points (0..10000). Integer math keeps every stored value exactly
    representable, so canonical JSON is byte-identical across languages."""
    def words(s: str) -> set[str]:
        return {w for w in re.split(r"[^A-Z0-9]+", s.upper()) if w}
    sw = words(supplier_name)
    if not sw:
        return 0
    return len(sw & words(narration)) * 10000 // len(sw)


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def edge_id(bank_txn_id: str, docs: list[dict]) -> str:
    """SPEC.md §1.6 rule 5: sha256 over {bank_txn_id, [doc_no, sign] sorted}."""
    preimage = {"bank_txn_id": bank_txn_id,
                "docs": sorted([d["doc_no"], d["sign"]] for d in docs)}
    return hashlib.sha256(_canonical(preimage)).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# subset search
# --------------------------------------------------------------------------- #

def _rank(target: int, subset: tuple[dict, ...]) -> tuple[int, int, tuple[str, ...]]:
    """Tie-break key: (|residual|, size, lexicographic doc_nos)."""
    signed = sum(_sign(d) * d["total_paise"] for d in subset)
    return (abs(target - signed), len(subset),
            tuple(sorted(d["inv_no"] for d in subset)))


def _sign(doc: dict) -> int:
    return -1 if doc["doc_type"] == "CDN" else 1


def _best_subset_brute(cands: list[dict], target: int):
    best = None
    for size in range(1, min(MAX_SUBSET_SIZE, len(cands)) + 1):
        for subset in combinations(cands, size):
            key = _rank(target, subset)
            if best is None or key < best[0]:
                best = (key, subset)
    return best


def _best_subset_mitm(cands: list[dict], target: int):
    """Meet-in-the-middle over two halves; equivalent result to brute force."""
    half = len(cands) // 2
    a_side, b_side = cands[:half], cands[half:]

    def subsets(side: list[dict]):
        out = [()]
        for size in range(1, min(MAX_SUBSET_SIZE, len(side)) + 1):
            out.extend(combinations(side, size))
        return out

    a_subsets = subsets(a_side)
    a_sums = sorted(
        (sum(_sign(d) * d["total_paise"] for d in s), i)
        for i, s in enumerate(a_subsets))
    sums_only = [s for s, _ in a_sums]

    best = None
    for b in subsets(b_side):
        if len(b) > MAX_SUBSET_SIZE:
            continue
        b_sum = sum(_sign(d) * d["total_paise"] for d in b)
        want = target - b_sum
        lo = bisect.bisect_left(sums_only, want - TOLERANCE_PAISE)
        hi = bisect.bisect_right(sums_only, want + TOLERANCE_PAISE)
        for _, idx in a_sums[lo:hi]:
            a = a_subsets[idx]
            subset = a + b
            if not subset or len(subset) > MAX_SUBSET_SIZE:
                continue
            key = _rank(target, subset)
            if best is None or key < best[0]:
                best = (key, subset)
    return best


def reconcile(docs: list[dict], bank_entries: list[dict]) -> list[dict]:
    """Match bank debits to signed document subsets. See module docstring for
    the determinism contract; inputs are GSTR2BRecord and BankEntry dicts."""
    pool = sorted(docs, key=lambda d: d["inv_no"])
    edges: list[dict] = []

    for entry in sorted(bank_entries,
                        key=lambda e: (e["value_date"], e["txn_id"])):
        if entry["direction"] != "DEBIT":
            continue  # supplier settlements are outgoing payments
        entry_date = datetime.date.fromisoformat(entry["value_date"])

        cands = []
        for doc in pool:
            sim = counterparty_similarity_bps(entry["narration"],
                                              doc["supplier_name"])
            days = abs((entry_date
                        - datetime.date.fromisoformat(doc["inv_date"])).days)
            if sim >= SIMILARITY_THRESHOLD_BPS and days <= WINDOW_DAYS:
                cands.append((doc, sim, days))
        if not cands:
            continue

        method = ("MEET_IN_MIDDLE" if len(cands) > MITM_THRESHOLD
                  else "EXACT_SUBSET")
        search = (_best_subset_mitm if method == "MEET_IN_MIDDLE"
                  else _best_subset_brute)
        best = search([c[0] for c in cands], entry["amount_paise"])
        if best is None or best[0][0] > TOLERANCE_PAISE:
            continue
        subset = sorted(best[1], key=lambda d: d["inv_no"])

        by_no = {c[0]["inv_no"]: c for c in cands}
        edge_docs = [{"doc_type": d["doc_type"], "doc_no": d["inv_no"],
                      "sign": _sign(d), "amount_paise": d["total_paise"]}
                     for d in subset]
        matched = sum(d["sign"] * d["amount_paise"] for d in edge_docs)
        edges.append({
            "edge_id": edge_id(entry["txn_id"], edge_docs),
            "bank_txn_id": entry["txn_id"],
            "docs": edge_docs,
            "matched_amount_paise": matched,
            "residual_paise": entry["amount_paise"] - matched,
            "method": method,
            "score": {
                "counterparty_similarity_bps": min(
                    by_no[d["inv_no"]][1] for d in subset),
                "max_day_distance": max(by_no[d["inv_no"]][2] for d in subset),
            },
        })
        matched_nos = {d["inv_no"] for d in subset}
        pool = [d for d in pool if d["inv_no"] not in matched_nos]

    return sorted(edges, key=lambda e: e["bank_txn_id"])
