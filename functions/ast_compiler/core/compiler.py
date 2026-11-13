"""OCR tokens -> typed Invoice AST with algebraic repair (SPEC.md §1.4–1.5, §2.2).

Pure Python, zero AWS imports. The pipeline is:

  1. spatial parse  — group tokens into rows, extract labeled fields, the items
                      table and the summary block, keeping token provenance;
  2. identity check — GSTIN structure + checksum (uniquely repairable when the
                      token is low-confidence, a SUPPLIER_ERROR when it is not);
  3. invariant pass — evaluate every statutory equation over integer paise;
  4. repair search  — for broken invariants, enumerate confusion-variants of
                      every low-confidence numeric token; exactly one survivor
                      -> OCR_REPAIRED with proof, otherwise NEEDS_REVIEW;
  5. rule checks    — tax head and rate validity are legal properties of the
                      document: violations are SUPPLIER_ERRORs, never repaired.

Raises ValueError for documents whose layout cannot be understood at all
(no items table, no GSTINs, ...): that is an ingestion failure, not a
compile result, and callers surface it as such.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import gstin as gstin_mod
from . import repair, tax_rules
from .money import TOLERANCE_PAISE, normalize_date, parse_inr, pct_to_bps

_INV_NO_RE = re.compile(r"Invoice\s*No\.?\s*:?\s*([A-Za-z0-9/_-]+)", re.I)
_DATE_RE = re.compile(r"Date\s*:?\s*([0-9][0-9/-]+)", re.I)
_GSTIN_LABEL_RE = re.compile(r"GSTIN\s*:?\s*([0-9A-Za-z]{15})", re.I)
_POS_RE = re.compile(r"Place\s*of\s*Supply\s*:?\s*(\d{2})", re.I)
_TAX_LABEL_RE = re.compile(r"^(CGST|SGST|IGST)\s*@\s*([0-9.]+)\s*%$", re.I)
_TAXABLE_LABEL_RE = re.compile(r"^Taxable\b", re.I)
_TOTAL_LABEL_RE = re.compile(r"^TOTAL\b")
_HSN_RE = re.compile(r"^\d{4,8}$")

_ROW_Y_TOLERANCE = 16  # px between y-centers that still count as one text row


@dataclass
class Cell:
    """A numeric value read from one OCR token, tracked for repair/provenance."""
    field: str        # AST field path, e.g. "line_items[1].unit_price_paise"
    text: str
    conf: float
    token_index: int
    kind: str         # "paise" | "int"

    def parse(self, text: str | None = None) -> int | None:
        t = self.text if text is None else text
        try:
            if self.kind == "paise":
                return parse_inr(t)
            if not re.fullmatch(r"\d+", t.replace(",", "")):
                return None
            return int(t.replace(",", ""))
        except ValueError:
            return None


def _rows(tokens: list[dict]) -> list[list[dict]]:
    """Group tokens into visual rows by y-center, each row sorted by x."""
    ordered = sorted(tokens, key=lambda t: (t["y"] + t["h"] / 2, t["x"]))
    rows: list[list[dict]] = []
    for tok in ordered:
        yc = tok["y"] + tok["h"] / 2
        if rows:
            row_yc = sum(t["y"] + t["h"] / 2 for t in rows[-1]) / len(rows[-1])
            if abs(yc - row_yc) <= _ROW_Y_TOLERANCE:
                rows[-1].append(tok)
                continue
        rows.append([tok])
    return [sorted(r, key=lambda t: t["x"]) for r in rows]


# --------------------------------------------------------------------------- #
# spatial parse
# --------------------------------------------------------------------------- #

@dataclass
class _Layout:
    inv_no: str
    inv_no_idx: int
    inv_date: str
    inv_date_idx: int
    supplier_name: str
    buyer_name: str
    supplier_gstin: dict      # token dict + extracted "value"
    buyer_gstin: dict
    place_of_supply: str
    pos_idx: int
    items: list[dict]         # per line: desc, hsn, qty/rate/amount Cells
    taxable: Cell
    tax_rows: list[tuple[str, int, Cell]]   # (head_part, bps, amount Cell)
    total: Cell


def _parse_layout(token_file: dict) -> _Layout:
    tokens = token_file["tokens"]
    rows = _rows(tokens)

    inv_no = inv_date = pos = None
    inv_no_idx = inv_date_idx = pos_idx = -1
    gstins: list[dict] = []
    for tok in tokens:
        if (m := _INV_NO_RE.search(tok["text"])) and inv_no is None:
            inv_no, inv_no_idx = m.group(1), tok["index"]
        if (m := _DATE_RE.search(tok["text"])) and inv_date is None:
            inv_date, inv_date_idx = m.group(1), tok["index"]
        if m := _GSTIN_LABEL_RE.search(tok["text"]):
            gstins.append({"value": m.group(1).upper(), "token": tok})
        if (m := _POS_RE.search(tok["text"])) and pos is None:
            pos, pos_idx = m.group(1), tok["index"]
    if inv_no is None or inv_date is None:
        raise ValueError("layout: invoice number/date not found")
    if len(gstins) != 2:
        raise ValueError(f"layout: expected 2 GSTIN labels, found {len(gstins)}")
    gstins.sort(key=lambda g: g["token"]["y"])  # topmost block is the supplier

    # supplier name: leftmost non-label token on the "Invoice No" row;
    # buyer name: leftmost non-label token on the row after "BILL TO"
    def leftmost_name(row: list[dict]) -> str | None:
        for tok in row:
            if not any(p.search(tok["text"])
                       for p in (_INV_NO_RE, _DATE_RE, _GSTIN_LABEL_RE, _POS_RE)):
                return tok["text"]
        return None

    supplier_name = buyer_name = None
    for i, row in enumerate(rows):
        row_text = " ".join(t["text"] for t in row)
        if _INV_NO_RE.search(row_text) and supplier_name is None:
            supplier_name = leftmost_name(row)
        if re.search(r"\bBILL\s*TO\b", row_text, re.I) and i + 1 < len(rows):
            buyer_name = leftmost_name(rows[i + 1])

    header_i = taxable_i = None
    for i, row in enumerate(rows):
        texts = [t["text"] for t in row]
        if header_i is None and "Description" in texts and "Qty" in texts:
            header_i = i
        if (taxable_i is None and header_i is not None and i > header_i
                and _TAXABLE_LABEL_RE.match(row[0]["text"])):
            taxable_i = i
    if header_i is None or taxable_i is None:
        raise ValueError("layout: items table or taxable row not found")

    items = []
    for row in rows[header_i + 1:taxable_i]:
        if len(row) < 5:
            continue  # separator noise / stray tokens
        amount_t, rate_t, qty_t, hsn_t = row[-1], row[-2], row[-3], row[-4]
        desc_tokens = row[1:-4] if len(row) >= 6 else row[:-4]
        i = len(items)
        items.append({
            "description": " ".join(t["text"] for t in desc_tokens) or f"line {i + 1}",
            "hsn": hsn_t["text"],
            "qty": Cell(f"line_items[{i}].qty", qty_t["text"], qty_t["conf"],
                        qty_t["index"], "int"),
            "unit_price": Cell(f"line_items[{i}].unit_price_paise", rate_t["text"],
                               rate_t["conf"], rate_t["index"], "paise"),
            "line_total": Cell(f"line_items[{i}].line_total_paise", amount_t["text"],
                               amount_t["conf"], amount_t["index"], "paise"),
        })
    if not items:
        raise ValueError("layout: no line items found")

    taxable = total = None
    tax_rows = []
    for row in rows[taxable_i:]:
        if len(row) < 2:
            continue
        label = " ".join(t["text"] for t in row[:-1])
        amt = row[-1]
        cell = Cell("", amt["text"], amt["conf"], amt["index"], "paise")
        if _TAXABLE_LABEL_RE.match(label) and taxable is None:
            cell.field = "taxable_value_paise"
            taxable = cell
        elif m := _TAX_LABEL_RE.match(label):
            part = m.group(1).upper()
            cell.field = f"tax.{part.lower()}_paise"
            tax_rows.append((part, pct_to_bps(m.group(2)), cell))
        elif _TOTAL_LABEL_RE.match(label) and total is None:
            cell.field = "total_paise"
            total = cell
    if taxable is None or total is None or not tax_rows:
        raise ValueError("layout: summary block (taxable/tax/total) incomplete")

    return _Layout(
        inv_no=inv_no, inv_no_idx=inv_no_idx,
        inv_date=normalize_date(inv_date), inv_date_idx=inv_date_idx,
        supplier_name=supplier_name or "UNKNOWN SUPPLIER",
        buyer_name=buyer_name or "UNKNOWN BUYER",
        supplier_gstin=gstins[0], buyer_gstin=gstins[1],
        place_of_supply=pos or "", pos_idx=pos_idx,
        items=items, taxable=taxable, tax_rows=tax_rows, total=total,
    )


# --------------------------------------------------------------------------- #
# invariants
# --------------------------------------------------------------------------- #

def _make_evaluator(layout: _Layout, head: str, rate_bps: int):
    """Returns evaluate(values) -> list of violation strings (empty == consistent).

    ``values`` maps every numeric Cell.field to an int, or None if unparseable.
    All arithmetic is integer; the rate check is scaled by 10000 to stay exact.
    """
    n = len(layout.items)

    def evaluate(values: dict[str, int | None]) -> list[str]:
        bad = [f"unparseable numeric field {f}" for f, v in values.items() if v is None]
        if bad:
            return bad
        v = values  # all ints from here on
        out: list[str] = []
        for i in range(n):
            q, p, t = (v[f"line_items[{i}].qty"],
                       v[f"line_items[{i}].unit_price_paise"],
                       v[f"line_items[{i}].line_total_paise"])
            if abs(q * p - t) > TOLERANCE_PAISE:  # discount column absent -> 0
                out.append(f"line_items[{i}]: {q} * {p} - 0 != {t} (paise)")
        line_sum = sum(v[f"line_items[{i}].line_total_paise"] for i in range(n))
        taxable = v["taxable_value_paise"]
        if abs(line_sum - taxable) > TOLERANCE_PAISE:
            out.append(f"sum(line_total)={line_sum} != taxable_value={taxable}")
        if head == "CGST_SGST":
            cgst, sgst = v["tax.cgst_paise"], v["tax.sgst_paise"]
            if abs(cgst - sgst) > TOLERANCE_PAISE:
                out.append(f"CGST={cgst} and SGST={sgst} are not equal halves")
            tax_total = cgst + sgst
        else:
            tax_total = v["tax.igst_paise"]
        if abs(taxable * rate_bps - tax_total * 10000) > TOLERANCE_PAISE * 10000:
            out.append(f"taxable={taxable} * {rate_bps}bps != tax_total={tax_total}")
        if abs(taxable + tax_total - v["total_paise"]) > TOLERANCE_PAISE:
            out.append(f"taxable={taxable} + tax={tax_total} != total={v['total_paise']}")
        return out

    return evaluate


def _numeric_cells(layout: _Layout) -> list[Cell]:
    cells: list[Cell] = []
    for item in layout.items:
        cells += [item["qty"], item["unit_price"], item["line_total"]]
    cells.append(layout.taxable)
    cells += [cell for _, _, cell in layout.tax_rows]
    cells.append(layout.total)
    return cells


# --------------------------------------------------------------------------- #
# repair search
# --------------------------------------------------------------------------- #

def _search_numeric_repair(cells: list[Cell], base_values: dict[str, int | None],
                           base_violations: list[str], evaluate):
    """Try every confusion-variant of every low-confidence numeric cell.

    Returns (survivors, proofs_by_field) where survivors is a list of
    (cell, candidate_text, value) that satisfy ALL invariants, and
    proofs_by_field holds the candidates_tried log per cell. The proof log
    lists the original, every single-substitution variant, and every satisfied
    candidate; uniqueness is decided over the full subset-substitution set.
    """
    survivors: list[tuple[Cell, str, int]] = []
    tried_log: dict[str, list[dict]] = {}
    eligible = [c for c in cells if repair.is_repair_eligible(c.conf)]
    for cell in sorted(eligible, key=lambda c: c.token_index):
        tried = [{"candidate": cell.text, "satisfied": False,
                  "reason": f"as-read violates: {base_violations[0]}"}]
        for cand in repair.variants(cell.text):
            if cand == cell.text:
                continue
            value = cell.parse(cand)
            if value is None:
                if repair.hamming(cand, cell.text) == 1:
                    tried.append({"candidate": cand, "satisfied": False,
                                  "reason": "non-numeric in numeric field"})
                continue
            trial = dict(base_values)
            trial[cell.field] = value
            violations = evaluate(trial)
            if not violations:
                survivors.append((cell, cand, value))
                tried.append({"candidate": cand, "satisfied": True,
                              "reason": "all invariants satisfied"})
            elif repair.hamming(cand, cell.text) == 1:
                tried.append({"candidate": cand, "satisfied": False,
                              "reason": violations[0]})
        tried_log[cell.field] = tried
    return survivors, tried_log


def _repair_gstin(entry: dict, who: str, errors: list[dict],
                  repairs: list[dict]) -> tuple[str, bool]:
    """Validate one GSTIN; uniquely repair a low-confidence misread via the
    checksum constraint. Returns (gstin_text, needs_review)."""
    value, tok = entry["value"], entry["token"]
    if gstin_mod.is_valid_gstin(value):
        return value, False
    if not repair.is_repair_eligible(tok["conf"]):
        errors.append({"class": "SUPPLIER_ERROR", "code": "GSTIN_CHECKSUM_FAIL",
                       "field": f"{who}.gstin",
                       "detail": f"GSTIN {value} fails structure/checksum "
                                 f"on a high-confidence read"})
        return value, False
    candidates = repair.variants(value)
    survivors = [c for c in candidates if c != value and gstin_mod.is_valid_gstin(c)]
    if len(survivors) != 1:
        return value, True  # zero or ambiguous -> NEEDS_REVIEW
    repaired = survivors[0]
    tried = [{"candidate": value, "satisfied": False,
              "reason": "as-read violates: GSTIN structure/checksum"}]
    tried += [{"candidate": c, "satisfied": c == repaired,
               "reason": ("structure and mod-36 checksum satisfied" if c == repaired
                          else "GSTIN structure/checksum not satisfied")}
              for c in candidates
              if c != value and (repair.hamming(c, value) == 1 or c == repaired)]
    repairs.append({"class": "OCR_REPAIRED", "field": f"{who}.gstin",
                    "original": value, "repaired": repaired,
                    "token_index": tok["index"], "token_conf": tok["conf"],
                    "candidates_tried": tried,
                    "constraint_satisfied":
                        f"mod36_checksum({repaired[:14]}) == {repaired[14]!r}"})
    return repaired, False


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #

def compile_invoice(token_file: dict) -> dict:
    """Compile an OCR token file (SPEC.md §2.1) into an Invoice AST (§2.2)."""
    layout = _parse_layout(token_file)
    errors: list[dict] = []
    repairs: list[dict] = []
    needs_review = False

    # ---- identities -------------------------------------------------------
    supplier_gstin, nr1 = _repair_gstin(layout.supplier_gstin, "supplier",
                                        errors, repairs)
    buyer_gstin, nr2 = _repair_gstin(layout.buyer_gstin, "buyer", errors, repairs)
    needs_review |= nr1 or nr2
    supplier_state, buyer_state = supplier_gstin[:2], buyer_gstin[:2]

    # ---- printed tax head and rate ---------------------------------------
    parts = {part for part, _, _ in layout.tax_rows}
    if "IGST" in parts and parts & {"CGST", "SGST"}:
        raise ValueError("layout: both IGST and CGST/SGST rows present")
    head = "IGST" if "IGST" in parts else "CGST_SGST"
    rate_bps = sum(bps for _, bps, _ in layout.tax_rows)

    # ---- numeric invariants + repair search -------------------------------
    cells = _numeric_cells(layout)
    evaluate = _make_evaluator(layout, head, rate_bps)
    values: dict[str, int | None] = {c.field: c.parse() for c in cells}
    violations = evaluate(values)

    if violations:
        eligible = [c for c in cells if repair.is_repair_eligible(c.conf)]
        if not eligible:
            if any(v.startswith("unparseable") for v in violations):
                needs_review = True  # confidently-read garbage: a human's call
            else:
                for v in violations:
                    errors.append({"class": "SUPPLIER_ERROR",
                                   "code": "ARITHMETIC_MISMATCH",
                                   "field": v.split(":")[0].split("=")[0].strip(),
                                   "detail": v})
        else:
            survivors, tried_log = _search_numeric_repair(
                cells, values, violations, evaluate)
            if len(survivors) == 1:
                cell, cand, value = survivors[0]
                values[cell.field] = value
                repairs.append({
                    "class": "OCR_REPAIRED", "field": cell.field,
                    "original": cell.text, "repaired": cand,
                    "token_index": cell.token_index, "token_conf": cell.conf,
                    "candidates_tried": tried_log[cell.field],
                    "constraint_satisfied": "; ".join([
                        "qty*unit_price - discount == line_total",
                        "sum(line_total) == taxable_value",
                        f"taxable_value * {rate_bps}bps == tax_total",
                        "taxable_value + tax_total == total (all integer paise, "
                        f"tolerance {TOLERANCE_PAISE}p)",
                    ]),
                })
                if evaluate(values):  # defensive: a unique survivor must be clean
                    needs_review = True
            else:
                needs_review = True  # zero or ambiguous survivors

    # ---- legal rule checks (SUPPLIER_ERROR, never repaired) ---------------
    expected = tax_rules.expected_tax_head(supplier_state, buyer_state)
    if head != expected:
        errors.append({
            "class": "SUPPLIER_ERROR", "code": "TAX_HEAD_MISMATCH",
            "field": "tax.head",
            "detail": f"document charges {head.replace('_', '+')} but supply "
                      f"{supplier_state}->{buyer_state} requires {expected}",
        })
    allowed = frozenset().union(*(
        tax_rules.allowed_rates_bps(layout.inv_date, hsn=item["hsn"])
        for item in layout.items))
    if rate_bps not in allowed:
        errors.append({
            "class": "SUPPLIER_ERROR", "code": "INVALID_RATE",
            "field": "tax.rate_bps",
            "detail": f"rate {rate_bps}bps is not a notified GST rate "
                      f"on {layout.inv_date}",
        })

    # ---- assemble AST -----------------------------------------------------
    def val(field: str) -> int:
        v = values.get(field)
        return v if v is not None else 0  # NEEDS_REVIEW ASTs carry best effort

    tax_amounts = {"cgst_paise": 0, "sgst_paise": 0, "igst_paise": 0}
    for part, _, cell in layout.tax_rows:
        tax_amounts[f"{part.lower()}_paise"] = val(cell.field)

    line_items = []
    provenance: dict[str, list[int]] = {
        "inv_no": [layout.inv_no_idx], "inv_date": [layout.inv_date_idx],
        "supplier.gstin": [layout.supplier_gstin["token"]["index"]],
        "buyer.gstin": [layout.buyer_gstin["token"]["index"]],
    }
    if layout.pos_idx >= 0:
        provenance["place_of_supply"] = [layout.pos_idx]
    for c in cells:
        provenance[c.field] = [c.token_index]
    for item in layout.items:
        line_items.append({
            "description": item["description"],
            "hsn": item["hsn"] if _HSN_RE.match(item["hsn"]) else "0000",
            "qty": max(1, val(item["qty"].field)),
            "unit_price_paise": val(item["unit_price"].field),
            "discount_paise": 0,
            "line_total_paise": val(item["line_total"].field),
        })

    return {
        "doc_type": "INV",
        "inv_no": layout.inv_no,
        "inv_date": layout.inv_date,
        "supplier": {"name": layout.supplier_name, "gstin": supplier_gstin,
                     "state_code": supplier_state},
        "buyer": {"name": layout.buyer_name, "gstin": buyer_gstin,
                  "state_code": buyer_state},
        "place_of_supply": layout.place_of_supply or buyer_state,
        "line_items": line_items,
        "taxable_value_paise": val("taxable_value_paise"),
        "tax": {"head": head, "rate_bps": rate_bps, **tax_amounts,
                "cess_paise": 0},
        "total_paise": val("total_paise"),
        "errors": errors,
        "repairs": [] if needs_review else repairs,
        "status": "NEEDS_REVIEW" if needs_review else "COMPILED",
        "field_provenance": provenance,
        "source": {"image_key": token_file.get("image", "")},
    }
