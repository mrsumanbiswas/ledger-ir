#!/usr/bin/env python3
"""Regenerate sample_data/ golden fixtures (deterministic; see SPEC.md §3).

Outputs:
  sample_data/invoice_882_flawed.png   PIL-rendered interstate bill, one blurred digit
  sample_data/invoice_882_tokens.json  precomputed OCR tokens (the blurred digit misread)
  sample_data/gstr2b_oct2026.json      normalized GSTR-2B dump (3 invoices + 1 credit note)
  sample_data/bank_oct2026.csv         bank statement with one lump-sum NEFT + decoys

All GSTINs are synthetic with valid mod-36 checksums; no real business identifiers.
This script carries its own copy of the checksum (reference in SPEC.md §1.3); the
solver implementation in functions/ast_compiler/core is developed test-first and must
not be imported here.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "sample_data"

C36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gstin_check(g: str) -> str:
    s = 0
    for i, ch in enumerate(g[:14]):
        p = C36.index(ch) * (1 if i % 2 == 0 else 2)
        s += p // 36 + p % 36
    return C36[(36 - s % 36) % 36]


def make_gstin(base14: str) -> str:
    assert len(base14) == 14
    return base14 + gstin_check(base14)


SUPPLIER_GSTIN = make_gstin("24ABCDE1234F1Z")  # Gujarat
BUYER_GSTIN = make_gstin("32FGHIJ5678K1Z")     # Kerala
SUPPLIER_NAME = "SHREE GANESH STEELS"
BUYER_NAME = "MALABAR FABRICATORS"


def font(size: int, bold: bool = False, mono: bool = False):
    names = (["DejaVuSansMono-Bold.ttf", "DejaVuSansMono.ttf"] if mono
             else ["DejaVuSans-Bold.ttf"] if bold else ["DejaVuSans.ttf"])
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def render_invoice() -> tuple[Image.Image, list[dict]]:
    W, H = 1000, 1400
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    tokens: list[dict] = []

    f_h1 = font(34, bold=True)
    f_lbl = font(20, bold=True)
    f_txt = font(20)
    f_num = font(20, mono=True)

    def put(text: str, x: int, y: int, fnt, conf: float = 0.97,
            read_as: str | None = None,
            anchor_right: int | None = None) -> tuple[int, int, int, int]:
        if anchor_right is not None:
            x = anchor_right - int(d.textlength(text, font=fnt))
        bbox = d.textbbox((x, y), text, font=fnt)
        d.text((x, y), text, font=fnt, fill="black")
        x0, y0, x1, y1 = bbox
        tokens.append({"index": len(tokens), "x": x0, "y": y0,
                       "w": max(1, x1 - x0), "h": max(1, y1 - y0),
                       "text": read_as if read_as is not None else text,
                       "conf": conf})
        return bbox

    y = 40
    put("TAX INVOICE", 380, y, f_h1)
    y += 70
    put(SUPPLIER_NAME, 60, y, f_lbl)
    put("Invoice No: INV-882", 640, y, f_txt)
    y += 32
    put("Plot 14, GIDC Estate, Vapi, Gujarat 396195", 60, y, f_txt)
    put("Date: 18-10-2026", 640, y, f_txt)
    y += 32
    put(f"GSTIN: {SUPPLIER_GSTIN}", 60, y, f_txt)
    y += 60
    put("BILL TO:", 60, y, f_lbl)
    y += 32
    put(BUYER_NAME, 60, y, f_txt)
    put("Place of Supply: 32-KERALA", 580, y, f_txt)
    y += 32
    put("Door 8/112, Industrial Area, Kochi, Kerala 682001", 60, y, f_txt)
    y += 32
    put(f"GSTIN: {BUYER_GSTIN}", 60, y, f_txt)
    y += 56

    # items table
    cols = {"sl": 60, "desc": 110, "hsn": 470, "qty": 580, "rate": 760, "amt": 930}
    d.line([(50, y - 8), (950, y - 8)], fill="black", width=2)
    put("Sl", cols["sl"], y, f_lbl)
    put("Description", cols["desc"], y, f_lbl)
    put("HSN", cols["hsn"], y, f_lbl)
    put("Qty", cols["qty"], y, f_lbl)
    put("Rate", 700, y, f_lbl)
    put("Amount", 860, y, f_lbl)
    y += 34
    d.line([(50, y - 6), (950, y - 6)], fill="black", width=1)

    blur_box = None
    rows = [
        ("1", "MS Steel Brackets", "7308", "10", "2,500.00", "25,000.00", None),
        # The "3" in 3,000.00 gets blurred; precomputed OCR misreads it as 8 (conf 0.62).
        ("2", "Mounting Rails", "7308", "5", "3,000.00", "15,000.00", "8,000.00"),
    ]
    for sl, desc, hsn, qty, rate, amt, read_as in rows:
        put(sl, cols["sl"], y, f_txt)
        put(desc, cols["desc"], y, f_txt)
        put(hsn, cols["hsn"], y, f_num)
        put(qty, cols["qty"], y, f_num)
        bbox = put(rate, 0, y, f_num, anchor_right=cols["rate"],
                   conf=0.62 if read_as else 0.97, read_as=read_as)
        if read_as:
            # bbox of the first character ("3") for localized blur
            ch_w = d.textlength(rate[0], font=f_num)
            blur_box = (bbox[0] - 2, bbox[1] - 2, bbox[0] + int(ch_w) + 2, bbox[3] + 2)
        put(amt, 0, y, f_num, anchor_right=cols["amt"])
        y += 36

    d.line([(50, y), (950, y)], fill="black", width=1)
    y += 20
    for label, val in [("Taxable Value", "40,000.00"),
                       ("CGST @ 9%", "3,600.00"),
                       ("SGST @ 9%", "3,600.00")]:
        put(label, 580, y, f_txt)
        put(val, 0, y, f_num, anchor_right=cols["amt"])
        y += 34
    d.line([(560, y), (950, y)], fill="black", width=2)
    y += 10
    put("TOTAL", 580, y, f_lbl)
    put("47,200.00", 0, y, f_num, anchor_right=cols["amt"])
    y += 60
    put("Rupees Forty Seven Thousand Two Hundred Only", 60, y, f_txt)
    y += 50
    put("Synthetic demo document - parties and GSTINs are not real.", 60, y, font(16))

    # blur the single digit so real OCR also sees it degraded
    assert blur_box is not None
    region = img.crop(blur_box)
    img.paste(region.filter(ImageFilter.GaussianBlur(radius=2.1)), blur_box)

    return img, tokens


def write_invoice_fixtures() -> None:
    img, tokens = render_invoice()
    img.save(OUT / "invoice_882_flawed.png")
    token_file = {
        "image": "invoice_882_flawed.png",
        "width": img.width,
        "height": img.height,
        "engine": "precomputed-fixture",
        "tokens": tokens,
    }
    (OUT / "invoice_882_tokens.json").write_text(
        json.dumps(token_file, indent=2, ensure_ascii=False) + "\n")


def write_gstr2b() -> None:
    def rec(doc_type, no, date, taxable, cgst=0, sgst=0, igst=0, **extra):
        r = {
            "doc_type": doc_type, "inv_no": no, "inv_date": date,
            "supplier_gstin": SUPPLIER_GSTIN, "supplier_name": SUPPLIER_NAME,
            "place_of_supply": "32", "taxable_value_paise": taxable,
            "rate_bps": 1800, "cgst_paise": cgst, "sgst_paise": sgst,
            "igst_paise": igst, "cess_paise": 0,
            "total_paise": taxable + cgst + sgst + igst,
            "itc_available": True,
        }
        r.update(extra)
        return r

    doc = {
        "period": "2026-10",
        "buyer_gstin": BUYER_GSTIN,
        "generated_on": "2026-11-14",
        "records": [
            rec("INV", "INV-881", "2026-10-05", 5_000_000, igst=900_000),
            # supplier's own filing mirrors the wrong CGST+SGST split on the bill
            rec("INV", "INV-882", "2026-10-18", 4_000_000, cgst=360_000, sgst=360_000),
            rec("INV", "INV-883", "2026-10-22", 3_000_000, igst=540_000),
            rec("CDN", "CN-12", "2026-10-25", 100_000, igst=18_000,
                original_inv_no="INV-881"),
        ],
    }
    (OUT / "gstr2b_oct2026.json").write_text(
        json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def write_bank_csv() -> None:
    rows = [
        ("UTIB2026100701", "2026-10-07", "UPI/DR/9876543210/TEA STALL", "UPI",
         "2500.00", "", "342500.00"),
        ("UTIB2026101502", "2026-10-15", "NEFT/CR/COCHIN TRADERS/PAYMENT OCT", "NEFT",
         "", "125000.00", "467500.00"),
        ("UTIB2026102803", "2026-10-28",
         "NEFT/DR/AXISN52026102812345/SHREE GANESH STEELS/OCT SETTLEMENT", "NEFT",
         "93220.00", "", "374280.00"),
        ("UTIB2026103004", "2026-10-30", "ACH/DR/RENT OCT WAREHOUSE", "ACH",
         "18000.00", "", "356280.00"),
    ]
    with (OUT / "bank_oct2026.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["txn_id", "value_date", "narration", "mode",
                    "debit_inr", "credit_inr", "balance_inr"])
        w.writerows(rows)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    assert gstin_check("27AAPFU0939F1ZV") == "V"
    assert gstin_check("29AAGCB7383J1Z4") == "4"
    assert 5_900_000 + 3_540_000 - 118_000 == 9_322_000  # golden NEFT equation
    write_invoice_fixtures()
    write_gstr2b()
    write_bank_csv()
    print("fixtures written to", OUT)
    print("supplier:", SUPPLIER_GSTIN, "| buyer:", BUYER_GSTIN)


if __name__ == "__main__":
    main()
