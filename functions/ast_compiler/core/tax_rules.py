"""Date-dependent GST rate sets and the tax-head rule (SPEC.md §1.1–1.2).

Pure Python, zero AWS imports. The allowed rate set switches on 2025-09-22
(SPEC.md §1.1), and tobacco/pan-masala HSNs keep 28% (+cess) after the switch.
"""
from __future__ import annotations

import re

RATE_SWITCH_DATE = "2025-09-22"
TOBACCO_HSN_PREFIXES = ("2401", "2402", "2403", "21069020")

_PRE_SWITCH = frozenset({0, 500, 1200, 1800, 2800, 300, 25})
_POST_SWITCH = frozenset({0, 500, 1800, 4000, 300, 25})

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def allowed_rates_bps(inv_date: str, hsn: str | None = None) -> frozenset[int]:
    """Allowed GST rates (basis points) for an invoice dated ``inv_date`` (ISO).

    ``hsn``, when given, widens the post-switch set for tobacco/pan-masala
    lines, which legally keep 28% (+cess) after the 2025-09-22 switch.
    """
    if not _ISO_RE.match(inv_date):
        raise ValueError(f"inv_date must be ISO YYYY-MM-DD, got {inv_date!r}")
    if inv_date < RATE_SWITCH_DATE:  # ISO strings compare chronologically
        return _PRE_SWITCH
    if hsn is not None and hsn.startswith(TOBACCO_HSN_PREFIXES):
        return _POST_SWITCH | {2800}
    return _POST_SWITCH


def expected_tax_head(supplier_state: str, buyer_state: str) -> str:
    """'CGST_SGST' when the states match, else 'IGST' (SPEC.md §1.2)."""
    return "CGST_SGST" if supplier_state == buyer_state else "IGST"
