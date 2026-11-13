"""GSTIN structure + mod-36 checksum validation (SPEC.md §1.3).

Pure Python, zero AWS imports. The checksum algorithm is verified against the
two real-format GSTINs quoted in SPEC.md §1.3.
"""
from __future__ import annotations

import re

C36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def gstin_check(gstin: str) -> str:
    """Mod-36 check character computed over the first 14 characters.

    Raises ValueError if those characters are not all in [0-9A-Z] or if the
    input is shorter than 14 characters.
    """
    if len(gstin) < 14:
        raise ValueError(f"GSTIN too short for checksum: {gstin!r}")
    s = 0
    for i, ch in enumerate(gstin[:14]):
        if ch not in C36:
            raise ValueError(f"invalid GSTIN character {ch!r} in {gstin!r}")
        p = C36.index(ch) * (1 if i % 2 == 0 else 2)
        s += p // 36 + p % 36
    return C36[(36 - s % 36) % 36]


def is_valid_gstin(gstin: str) -> bool:
    """Full validity: 15 chars, structural pattern, and checksum match."""
    if not isinstance(gstin, str) or not GSTIN_RE.match(gstin):
        return False
    return gstin_check(gstin) == gstin[14]


def state_code(gstin: str) -> str:
    """First two characters (the numeric state code). Validates first."""
    if not is_valid_gstin(gstin):
        raise ValueError(f"not a valid GSTIN: {gstin!r}")
    return gstin[:2]
