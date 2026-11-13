# SPEC.md — LedgerIR data contracts

Status: **implemented through Phase 2** (sample data + pure solvers). This file is
the source of truth for LedgerIR's data contracts and domain rules; where earlier
planning notes disagreed (rate sets, interest wording, error classes, tolerance),
the rules recorded here win.

Conventions used by every schema below:

- **Money is integer paise** (`*_paise` fields). Floats never hold money. Rounding
  tolerance everywhere: **≤ 100 paise**.
- **Rates are integer basis points** (`rate_bps`): 18% → `1800`, 0.25% → `25`.
- Dates are ISO `YYYY-MM-DD` strings; periods are `YYYY-MM`.
- **Canonical JSON** (for hashing, edge ids, Merkle leaves):
  `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`.
- JSON Schemas are draft 2020-12. Each schema's `$id` is stable; tests extract these
  exact blocks from this file by `$id` and validate the fixtures against them.
  Fields not listed are forbidden (`additionalProperties: false`) unless noted.

---

## 1. Domain rules (resolved)

### 1.1 GST rate validity by invoice date

| invoice_date | allowed `rate_bps` |
|---|---|
| < 2025-09-22 | 0, 500, 1200, 1800, 2800 + special 300, 25 |
| ≥ 2025-09-22 | 0, 500, 1800, 4000 + special 300, 25 |
| ≥ 2025-09-22, tobacco/pan-masala HSN (prefixes `2401`, `2402`, `2403`, `21069020`) | 2800 additionally allowed (+ cess) |

The PDF's fixed set `{0,5,12,18,28}` is superseded. A rate outside the allowed set
for the invoice date is a **SUPPLIER_ERROR** (`INVALID_RATE`), never an OCR repair
target on its own.

### 1.2 Tax head

State code = first 2 characters of GSTIN. `supplier_state == buyer_state` →
**CGST + SGST in equal halves** (each = `taxable_value * rate_bps / 2 / 10000`,
integer paise, ≤100p tolerance). Otherwise → **IGST**. A document whose printed tax
head contradicts this rule has a **SUPPLIER_ERROR** (`TAX_HEAD_MISMATCH`). We never
"repair" the head.

### 1.3 GSTIN

15 chars: `[0-9]{2}` state + PAN `[A-Z]{5}[0-9]{4}[A-Z]` + entity `[1-9A-Z]` + `Z` +
mod-36 check char (verified:
`gstin_check("27AAPFU0939F1ZV") == "V"`, `gstin_check("29AAGCB7383J1Z4") == "4"`).
All fixture GSTINs are synthetic with valid checksums; never a real business's GSTIN.
Fixture identities:

| Party | GSTIN | State |
|---|---|---|
| Supplier: SHREE GANESH STEELS (synthetic) | `24ABCDE1234F1Z6` | 24 Gujarat |
| Buyer: MALABAR FABRICATORS (synthetic) | `32FGHIJ5678K1ZA` | 32 Kerala |

### 1.4 Invariants (per invoice)

1. For every line: `qty * unit_price_paise - discount_paise == line_total_paise`.
2. `sum(line_total_paise) == taxable_value_paise`.
3. `taxable_value_paise * rate_bps / 10000 == cgst_paise + sgst_paise` (intrastate)
   or `== igst_paise` (interstate), within 100 paise.
4. `taxable_value_paise + cgst + sgst + igst + cess == total_paise`, within 100 paise.

### 1.5 Error classes — never mixed

| Class | Meaning | Output |
|---|---|---|
| `OCR_REPAIRED` | *We* misread a character; the paper document is internally consistent. | Repair proof object (§2.6); AST uses repaired value. |
| `SUPPLIER_ERROR` | The legal document itself is wrong. | Error record on the AST; drives IMS action / payment hold. **Never alter the document's values.** |

**Repair uniqueness rule:** a repair is accepted only when **exactly one** candidate
satisfies *all* invariants. Zero or multiple survivors → invoice `status =
NEEDS_REVIEW`, no repair emitted. Only tokens with `conf < 0.9` are repair-eligible.
Confusion alphabet (bidirectional): `8↔B`, `5↔S`, `3↔8`, `0↔O`, `1↔I`, `1↔7`
(so `8→{3,B}`, `3→{8}`, `1→{I,7}`, etc.).
**Candidate generation:** for a low-conf token, the candidate set is every variant
obtained by substituting any subset of its confusable characters with a confusion
partner, plus the as-read original. Tokens are short, so the set is small and fully
enumerable — no heuristics. A candidate that is non-numeric in a numeric field is
recorded as tried-and-invalid (`satisfied: false`). Survivors are candidates that
satisfy all invariants within the 100-paise tolerance; note the tolerance means two
numerically close variants can both survive — that is ambiguity, and it correctly
yields `NEEDS_REVIEW`. An invariant violation involving only high-confidence tokens
(`conf ≥ 0.9`) is the supplier's arithmetic, not our reading:
`SUPPLIER_ERROR / ARITHMETIC_MISMATCH`, no repair attempted.

Implementation bounds (deterministic): a token with more than 4096 variants is
declared unrepairable (surfacing as `NEEDS_REVIEW` when an invariant is broken).
Uniqueness is always decided over the **full** variant set; the proof's
`candidates_tried` lists the as-read original, every single-substitution variant,
and every satisfied candidate, so proofs stay small without weakening the claim.
GSTINs get the same treatment with the structure + mod-36 checksum as the
constraint (a low-confidence misread GSTIN with exactly one valid variant is an
`OCR_REPAIRED`; a high-confidence invalid GSTIN is
`SUPPLIER_ERROR / GSTIN_CHECKSUM_FAIL`).

### 1.6 Reconciler

1. Filter bank-entry candidates to the same counterparty (token-similarity on
   narration vs supplier name) and a **±90-day** window around the document dates.
2. Search signed subsets — invoices `+1`, credit notes `−1` — up to **size 5**;
   switch to meet-in-the-middle when > 20 candidate documents.
3. A match is exact when `|residual_paise| == 0`; acceptable when `≤ 100`.
4. **Deterministic tie-breaking:** among equal-residual subsets prefer (a) smaller
   subset size, (b) lexicographically smallest sorted tuple of `doc_no`s. Same input
   → same edges, always.
5. `edge_id` = first 16 hex chars of SHA-256 over canonical JSON of
   `{"bank_txn_id": ..., "docs": [[doc_no, sign], ...sorted by doc_no]}`.
6. Only `DEBIT` entries are candidate settlements (supplier payments are
   outgoing); bank entries are processed in `(value_date, txn_id)` order and
   each document settles at most one entry (greedy, deterministic).

### 1.7 IMS triage decision table

Evaluated **top-down, first match wins** (precedence: REJECT > HOLD_GST >
AUTO_ACCEPT > AUTO_PENDING, i.e. a fault always outranks acceptance, and acceptance
requires positive evidence — otherwise the safe default is PENDING). Input: compiled
AST (if any), GSTR-2B record (if any), match edges.

| # | Condition | Action | Holdback |
|---|---|---|---|
| 1 | 2B record's GSTIN fails checksum, or duplicate `inv_no` for the same supplier+period | `REJECT` | 0 |
| 2 | Any `SUPPLIER_ERROR` on the compiled AST, or AST↔2B tax-head disagreement | `HOLD_GST` | total tax paise (cgst+sgst+igst+cess) |
| 3 | 2B record clean AND consistent with its AST within 100 paise (when an AST exists, `status == COMPILED`) AND either (a) an AST exists, or (b) the document is settled by a match edge with `|residual_paise| ≤ 100` | `AUTO_ACCEPT` | 0 |
| 4 | Otherwise (AST `NEEDS_REVIEW`, missing counterpart with no settlement, amount mismatch > 100 paise, unmatched) | `AUTO_PENDING` | 0 |

Holdback = the GST amount withheld from the supplier payment ("18% GST holdback" =
the tax portion of the bill, 18% being the headline rate).

Interest on wrongly availed **and utilised** ITC is stated as
**"18% (as notified; statutory ceiling 24%)"** — never flatly 24%.

### 1.8 Merkle dossier

SHA-256 over canonical JSON. Leaves = every pipeline record (ASTs, 2B records, bank
entries, match edges, IMS results) **sorted by `SK`** (the DynamoDB sort key; records
not yet persisted use their would-be SK, see §4). Odd node at a
level is promoted (hashed with itself is **not** used; it carries up unchanged).
Internal node = `sha256(left + right)` over the 32-byte digests. Same input ⇒ same
root, regardless of input ordering.

---

## 2. JSON Schemas

### 2.1 OCR token file (`edge_ocr` output)

One file per image. Coordinates are pixels in the source image; `conf` ∈ [0,1].
Tokens are emitted in raster order (top-to-bottom, left-to-right) and `index` is the
position in that order (stable reference for provenance and proofs).

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://ledger-ir/spec/ocr_tokens.schema.json",
  "title": "OCRTokenFile",
  "type": "object",
  "additionalProperties": false,
  "required": ["image", "width", "height", "tokens"],
  "properties": {
    "image": { "type": "string", "description": "source image filename or S3 key" },
    "width": { "type": "integer", "minimum": 1 },
    "height": { "type": "integer", "minimum": 1 },
    "engine": { "type": "string", "description": "e.g. rapidocr-onnxruntime/<ver> or 'precomputed-fixture'" },
    "tokens": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["index", "x", "y", "w", "h", "text", "conf"],
        "properties": {
          "index": { "type": "integer", "minimum": 0 },
          "x": { "type": "integer", "minimum": 0 },
          "y": { "type": "integer", "minimum": 0 },
          "w": { "type": "integer", "minimum": 1 },
          "h": { "type": "integer", "minimum": 1 },
          "text": { "type": "string", "minLength": 1 },
          "conf": { "type": "number", "minimum": 0, "maximum": 1 }
        }
      }
    }
  }
}
```

### 2.2 Invoice AST (`ast_compiler` output)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://ledger-ir/spec/invoice_ast.schema.json",
  "title": "InvoiceAST",
  "type": "object",
  "additionalProperties": false,
  "required": ["doc_type", "inv_no", "inv_date", "supplier", "buyer",
               "place_of_supply", "line_items", "taxable_value_paise", "tax",
               "total_paise", "errors", "repairs", "status"],
  "properties": {
    "doc_type": { "const": "INV" },
    "inv_no": { "type": "string", "minLength": 1 },
    "inv_date": { "type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$" },
    "supplier": { "$ref": "#/$defs/party" },
    "buyer": { "$ref": "#/$defs/party" },
    "place_of_supply": { "type": "string", "pattern": "^\\d{2}$" },
    "line_items": {
      "type": "array", "minItems": 1,
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["description", "hsn", "qty", "unit_price_paise",
                     "discount_paise", "line_total_paise"],
        "properties": {
          "description": { "type": "string" },
          "hsn": { "type": "string", "pattern": "^\\d{4,8}$" },
          "qty": { "type": "integer", "minimum": 1 },
          "unit_price_paise": { "type": "integer", "minimum": 0 },
          "discount_paise": { "type": "integer", "minimum": 0 },
          "line_total_paise": { "type": "integer", "minimum": 0 }
        }
      }
    },
    "taxable_value_paise": { "type": "integer", "minimum": 0 },
    "tax": {
      "type": "object",
      "additionalProperties": false,
      "required": ["head", "rate_bps", "cgst_paise", "sgst_paise",
                   "igst_paise", "cess_paise"],
      "properties": {
        "head": { "enum": ["CGST_SGST", "IGST"] },
        "rate_bps": { "type": "integer", "minimum": 0 },
        "cgst_paise": { "type": "integer", "minimum": 0 },
        "sgst_paise": { "type": "integer", "minimum": 0 },
        "igst_paise": { "type": "integer", "minimum": 0 },
        "cess_paise": { "type": "integer", "minimum": 0 }
      }
    },
    "total_paise": { "type": "integer", "minimum": 0 },
    "errors": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["class", "code", "field", "detail"],
        "properties": {
          "class": { "const": "SUPPLIER_ERROR" },
          "code": { "enum": ["TAX_HEAD_MISMATCH", "INVALID_RATE",
                             "GSTIN_CHECKSUM_FAIL", "ARITHMETIC_MISMATCH"] },
          "field": { "type": "string" },
          "detail": { "type": "string" }
        }
      }
    },
    "repairs": {
      "type": "array",
      "items": { "$ref": "https://ledger-ir/spec/repair_proof.schema.json" }
    },
    "status": { "enum": ["COMPILED", "NEEDS_REVIEW"] },
    "field_provenance": {
      "type": "object",
      "description": "AST field path -> OCR token index(es) it was read from",
      "additionalProperties": { "type": "array", "items": { "type": "integer" } }
    },
    "source": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "image_key": { "type": "string" },
        "tokens_key": { "type": "string" }
      }
    }
  },
  "$defs": {
    "party": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "gstin", "state_code"],
      "properties": {
        "name": { "type": "string", "minLength": 1 },
        "gstin": { "type": "string", "pattern": "^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$" },
        "state_code": { "type": "string", "pattern": "^\\d{2}$" }
      }
    }
  }
}
```

### 2.3 GSTR-2B record (normalized; one per document)

The fixture file `sample_data/gstr2b_oct2026.json` is
`{"period", "buyer_gstin", "generated_on", "records": [GSTR2BRecord...]}` where each
record validates against this schema. **Resolved fixture content** (the PDF's
"3 records" means the three *invoice* records; the credit note rides along as a
fourth record of `doc_type: "CDN"`, required for the golden bank match):
`INV-881`, `INV-882` (mirrors the supplier's wrong CGST+SGST split), `INV-883`,
`CN-12` (against INV-881).

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://ledger-ir/spec/gstr2b_record.schema.json",
  "title": "GSTR2BRecord",
  "type": "object",
  "additionalProperties": false,
  "required": ["doc_type", "inv_no", "inv_date", "supplier_gstin", "supplier_name",
               "place_of_supply", "taxable_value_paise", "rate_bps", "cgst_paise",
               "sgst_paise", "igst_paise", "cess_paise", "total_paise",
               "itc_available"],
  "properties": {
    "doc_type": { "enum": ["INV", "CDN"] },
    "inv_no": { "type": "string", "minLength": 1 },
    "inv_date": { "type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$" },
    "original_inv_no": { "type": "string", "description": "CDN only: the invoice it credits" },
    "supplier_gstin": { "type": "string", "pattern": "^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$" },
    "supplier_name": { "type": "string" },
    "place_of_supply": { "type": "string", "pattern": "^\\d{2}$" },
    "taxable_value_paise": { "type": "integer", "minimum": 0 },
    "rate_bps": { "type": "integer", "minimum": 0 },
    "cgst_paise": { "type": "integer", "minimum": 0 },
    "sgst_paise": { "type": "integer", "minimum": 0 },
    "igst_paise": { "type": "integer", "minimum": 0 },
    "cess_paise": { "type": "integer", "minimum": 0 },
    "total_paise": { "type": "integer", "minimum": 0 },
    "itc_available": { "type": "boolean" },
    "ims_action": { "enum": ["AUTO_ACCEPT", "AUTO_PENDING", "HOLD_GST", "REJECT", null] }
  }
}
```

### 2.4 Bank entry (parsed from CSV)

CSV columns (fixture `bank_oct2026.csv`, header row):
`txn_id,value_date,narration,mode,debit_inr,credit_inr,balance_inr` — INR decimals
in the CSV; the parser converts to integer paise and emits this record:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://ledger-ir/spec/bank_entry.schema.json",
  "title": "BankEntry",
  "type": "object",
  "additionalProperties": false,
  "required": ["txn_id", "value_date", "narration", "mode", "direction", "amount_paise"],
  "properties": {
    "txn_id": { "type": "string", "minLength": 1 },
    "value_date": { "type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$" },
    "narration": { "type": "string" },
    "mode": { "enum": ["NEFT", "UPI", "IMPS", "RTGS", "ACH", "OTHER"] },
    "direction": { "enum": ["DEBIT", "CREDIT"] },
    "amount_paise": { "type": "integer", "minimum": 1 },
    "balance_paise": { "type": "integer" }
  }
}
```

### 2.5 Match edge (`graph_reconciler` output)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://ledger-ir/spec/match_edge.schema.json",
  "title": "MatchEdge",
  "type": "object",
  "additionalProperties": false,
  "required": ["edge_id", "bank_txn_id", "docs", "matched_amount_paise",
               "residual_paise", "method", "score"],
  "properties": {
    "edge_id": { "type": "string", "pattern": "^[0-9a-f]{16}$" },
    "bank_txn_id": { "type": "string" },
    "docs": {
      "type": "array", "minItems": 1, "maxItems": 5,
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["doc_type", "doc_no", "sign", "amount_paise"],
        "properties": {
          "doc_type": { "enum": ["INV", "CDN"] },
          "doc_no": { "type": "string" },
          "sign": { "enum": [1, -1] },
          "amount_paise": { "type": "integer", "minimum": 0 }
        }
      }
    },
    "matched_amount_paise": { "type": "integer", "minimum": 0 },
    "residual_paise": { "type": "integer" },
    "method": { "enum": ["EXACT_SUBSET", "MEET_IN_MIDDLE"] },
    "score": {
      "type": "object",
      "additionalProperties": false,
      "required": ["counterparty_similarity", "max_day_distance"],
      "properties": {
        "counterparty_similarity": { "type": "number", "minimum": 0, "maximum": 1 },
        "max_day_distance": { "type": "integer", "minimum": 0 }
      }
    }
  }
}
```

Semantics: `matched_amount_paise = sum(sign * doc.amount_paise)` and
`residual_paise = bank_amount_paise − matched_amount_paise`. Credit notes carry
`sign: -1`.

### 2.6 Repair proof (`OCR_REPAIRED` evidence)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://ledger-ir/spec/repair_proof.schema.json",
  "title": "RepairProof",
  "type": "object",
  "additionalProperties": false,
  "required": ["class", "field", "original", "repaired", "token_index",
               "token_conf", "candidates_tried", "constraint_satisfied"],
  "properties": {
    "class": { "const": "OCR_REPAIRED" },
    "field": { "type": "string", "description": "AST field path, e.g. line_items[1].unit_price_paise" },
    "original": { "type": "string", "description": "raw OCR text as read" },
    "repaired": { "type": "string", "description": "accepted replacement text" },
    "token_index": { "type": "integer", "minimum": 0 },
    "token_conf": { "type": "number", "minimum": 0, "maximum": 1, "description": "must be < 0.9" },
    "candidates_tried": {
      "type": "array", "minItems": 1,
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["candidate", "satisfied", "reason"],
        "properties": {
          "candidate": { "type": "string" },
          "satisfied": { "type": "boolean" },
          "reason": { "type": "string", "description": "equation result or 'non-numeric in numeric field'" }
        }
      }
    },
    "constraint_satisfied": { "type": "string", "description": "the invariant(s) the unique candidate satisfies, as an equation string" }
  }
}
```

Exactly one element of `candidates_tried` has `satisfied: true`, and it equals
`repaired` — enforced by the uniqueness rule, asserted by tests.

---

## 3. Golden fixtures (exact numbers)

All parties, GSTINs and amounts are synthetic. Period `2026-10`; post-2025-09-22
rate regime, so 18% (`1800` bps) is valid.

### 3.1 `invoice_882_flawed.png` (+ `invoice_882_tokens.json` precomputed OCR fallback)

Interstate bill, supplier Gujarat (24) → buyer Kerala (32), dated **2026-10-18**.

| Line | Description | HSN | Qty | Unit price | Line total |
|---|---|---|---|---|---|
| 1 | MS Steel Brackets | 7308 | 10 | ₹2,500.00 | ₹25,000.00 |
| 2 | Mounting Rails | 7308 | 5 | ₹3,000.00 | ₹15,000.00 |

Taxable ₹40,000.00; printed tax: **CGST @9% ₹3,600.00 + SGST @9% ₹3,600.00**
(the supplier's legal mistake — interstate must be IGST @18% ₹7,200.00);
Total ₹47,200.00.

Two flaws, one of each class:

1. **OCR flaw** (ours): the `3` in line 2's unit price `3,000.00` is blurred in the
   image; the precomputed token reads `8,000.00` with `conf 0.62`. Candidates for
   the flagged char: `8` (as-read, 5×8,000=40,000 ≠ 15,000 ✗), `3`
   (5×3,000=15,000 ✓ and Σlines=40,000 ✓), `B` (non-numeric ✗). Exactly one
   survivor → `OCR_REPAIRED`, `repaired` value `3,000.00`
   (`unit_price_paise = 300000`).
2. **Supplier flaw** (theirs): CGST+SGST on an interstate bill →
   `SUPPLIER_ERROR / TAX_HEAD_MISMATCH` → IMS `HOLD_GST`, holdback **₹7,200 =
   720,000 paise**.

Expected AST: `status COMPILED`, exactly 1 repair, exactly 1 error, invariants hold
after repair.

### 3.2 `gstr2b_oct2026.json`

| doc | type | date | taxable | tax | total |
|---|---|---|---|---|---|
| INV-881 | INV | 2026-10-05 | ₹50,000 | IGST ₹9,000 | **₹59,000** |
| INV-882 | INV | 2026-10-18 | ₹40,000 | CGST ₹3,600 + SGST ₹3,600 | ₹47,200 |
| INV-883 | INV | 2026-10-22 | ₹30,000 | IGST ₹5,400 | **₹35,400** |
| CN-12 | CDN (vs INV-881) | 2026-10-25 | ₹1,000 | IGST ₹180 | **₹1,180** |

All from supplier `24ABCDE1234F1Z6`, POS `32`, rate 1800 bps.

### 3.3 `bank_oct2026.csv`

| txn_id | date | mode | dir | amount | narration contains |
|---|---|---|---|---|---|
| UTIB2026100701 | 2026-10-07 | UPI | DEBIT | ₹2,500.00 | `TEA STALL` (decoy) |
| UTIB2026101502 | 2026-10-15 | NEFT | CREDIT | ₹1,25,000.00 | `COCHIN TRADERS` (decoy) |
| **UTIB2026102803** | 2026-10-28 | NEFT | DEBIT | **₹93,220.00** | `SHREE GANESH STEELS` |
| UTIB2026103004 | 2026-10-30 | ACH | DEBIT | ₹18,000.00 | `RENT OCT` (decoy) |

Golden match: `9322000 = 5900000 (INV-881) + 3540000 (INV-883) − 118000 (CN-12)`,
**residual 0**, method `EXACT_SUBSET`, docs sorted `CN-12, INV-881, INV-883` in the
edge-id preimage.

### 3.4 Pipeline-level

- IMS: INV-882 → `HOLD_GST` (holdback 720000); INV-881, INV-883, CN-12 clean & settled → `AUTO_ACCEPT`.
- Merkle root over the full record set is **identical across two runs** and
  **independent of input ordering**.

---

## 4. DynamoDB keys

| Entity | PK | SK |
|---|---|---|
| Invoice AST | `TENANT#<id>#<YYYY-MM>` | `DOC#INV#<inv_no>` |
| GSTR-2B record | same | `DOC#GSTR2B#<inv_no>` |
| Bank entry | same | `DOC#BANK#<txn_id>` |
| Match edge | same | `MATCH#<edge_id>` |
| Monthly summary | same | `SUMMARY#MONTHLY` |

Merkle leaves sort by these SKs (§1.8).

---

## 5. Test ↔ assertion map (Phase 2 gate)

| Golden assertion | Test |
|---|---|
| GSTIN checksum algorithm + fixture GSTINs valid/synthetic | `tests/test_gstin.py` |
| Rate sets by date; tax-head rule | `tests/test_tax_rules.py` |
| invoice_882: exactly one `OCR_REPAIRED` with full proof | `tests/test_ast_compiler_golden.py` |
| invoice_882: exactly one `SUPPLIER_ERROR` (tax head), never repaired | `tests/test_ast_compiler_golden.py` |
| Repair uniqueness rule (0 or >1 candidates → `NEEDS_REVIEW`) | `tests/test_ast_compiler_golden.py` |
| NEFT ₹93,220 = INV-881 + INV-883 − CN-12, residual 0, deterministic | `tests/test_reconciler_golden.py` |
| IMS: 882 → `HOLD_GST` + 18% holdback; decision table rows | `tests/test_ims_triage.py` |
| Identical Merkle root across runs / orderings | `tests/test_merkle.py` |
| Fixtures validate against the schemas in this file | `tests/test_fixtures.py` |
| GSTIN checksum repair, MITM path, CSV strictness, money parsing | `tests/test_edge_cases.py` |
| Full offline pipeline: golden IMS outcomes + identical root across two runs | `tests/test_pipeline_offline.py` |

Solvers live in `functions/ast_compiler/core/` and `functions/graph_reconciler/core/`
(pure Python, zero AWS imports — enforced by review and by the fact that the whole
suite runs offline). Phase 2 status: **implemented, 57/57 tests green**, lint-clean
(`ruff --select E,F,W,B,UP,SIM`).
