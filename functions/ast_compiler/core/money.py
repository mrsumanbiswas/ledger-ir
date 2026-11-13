"""Integer-paise money parsing and shared numeric conventions (SPEC.md intro).

Money never touches float. All parsers raise ValueError on anything that is not
a well-formed amount, so callers can treat "unparseable" as a first-class state
(it is what makes a token a repair candidate rather than silently wrong data).
"""
from __future__ import annotations

import datetime
import re

TOLERANCE_PAISE = 100

# "2500", "2,500.00", "1,25,000.00" (Indian grouping), optional leading rupee sign
_INR_RE = re.compile(r"^(?:\d{1,3}(?:,\d{2,3})*|\d+)(?:\.\d{2})?$")


def parse_inr(text: str) -> int:
    """'93,220.00' / '93220.00' / '93220' -> 9322000 (integer paise)."""
    t = text.strip().removeprefix("₹").strip()
    if not _INR_RE.match(t):
        raise ValueError(f"not an INR amount: {text!r}")
    t = t.replace(",", "")
    if "." in t:
        rupees, paise = t.split(".")
        return int(rupees) * 100 + int(paise)
    return int(t) * 100


def pct_to_bps(text: str) -> int:
    """'9' -> 900, '18' -> 1800, '0.25' -> 25 (basis points, integer math)."""
    t = text.strip().removesuffix("%").strip()
    if "." in t:
        whole, frac = t.split(".", 1)
        frac = (frac + "00")[:2]
        if not ((whole.isdigit() or whole == "") and frac.isdigit()):
            raise ValueError(f"not a percentage: {text!r}")
        return int(whole or "0") * 100 + int(frac)
    if not t.isdigit():
        raise ValueError(f"not a percentage: {text!r}")
    return int(t) * 100


_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DMY_RE = re.compile(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$")


def normalize_date(text: str) -> str:
    """'18-10-2026' (Indian DD-MM-YYYY) or '2026-10-18' -> '2026-10-18'.

    Validates that the date exists; raises ValueError otherwise.
    """
    t = text.strip()
    if m := _ISO_RE.match(t):
        y, mo, d = (int(g) for g in m.groups())
    elif m := _DMY_RE.match(t):
        d, mo, y = (int(g) for g in m.groups())
    else:
        raise ValueError(f"not a date: {text!r}")
    return datetime.date(y, mo, d).isoformat()
