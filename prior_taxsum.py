#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
taxsum.py  –  Tax Summary Generator
Usage:  python taxsum.py  taxin.pdf [--debug]
Produces:  taxin_taxsum.pdf

Sections produced:
  1. Filing Info (filing status, tax year, names)
  2. Income Breakdown table (ordinary dividends, qualified dividends, IRA withdrawal, % of total)
  3. Tax Breakdown (QD&CG Tax Worksheet lines: 15%, 20%, ordinary brackets)
  4. Ordinary Bracket Breakdown (using tax bracket table by filing status / year)
  5. NIIT (Form 8960)
  6. Foreign Tax Credit (Form 1116)
  7. Tax Payment Schedule (Form 2210 / Schedule AI periods + required + paid)
  8. Remaining Tax Owed / Refund
"""

import sys, os, re, json, io, math
import pdfplumber
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
    KeepTogether, Image
)
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT

# Global verbosity flag — set to True by --debug
_verbose = False

MIN_YEAR = 2020
MAX_YEAR = 2025

def vprint(*args, **kwargs):
    if _verbose:
        print(*args, **kwargs)


# ─────────────────────────────────────────────────────────────
#  1. PDF TEXT EXTRACTION
# ─────────────────────────────────────────────────────────────

def extract_all_text(pdf_path):
    """Return dict: {page_number (1-based): text_string}"""
    pages = {}
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            pages[i + 1] = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
    return pages


# ─────────────────────────────────────────────────────────────
#  2. HELPERS
# ─────────────────────────────────────────────────────────────

def parse_number(raw):
    """Convert strings like '1,234' / '(5,678)' / '-1234' to float."""
    if raw is None:
        return None
    raw = str(raw).strip().replace(",", "").replace("$", "").replace(" ", "")
    negative = False
    if raw.startswith("(") and raw.endswith(")"):
        negative = True
        raw = raw[1:-1]
    elif raw.startswith("-"):
        negative = True
        raw = raw[1:]
    try:
        val = float(raw)
        return -val if negative else val
    except ValueError:
        return None


def last_number_on_line(line):
    """Return the rightmost dollar-significant number from a text line."""
    nums = re.findall(r"\([\d,]+(?:\.\d+)?\)|[\d,]+(?:\.\d+)?", line)
    real = []
    for n in nums:
        bare = n.replace("(", "").replace(")", "").replace(",", "")
        if "," in n or (len(bare) >= 4 and "." not in n) or "." in n:
            v = parse_number(n)
            if v is not None:
                real.append(v)
    return real[-1] if real else None


def find_line_value(text, label_re, default=0.0):
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if re.search(label_re, line, re.IGNORECASE):
            v = last_number_on_line(line)
            if v is not None:
                return v
            for j in range(i + 1, min(i + 3, len(lines))):
                nxt = lines[j].strip()
                if nxt:
                    v = last_number_on_line(nxt)
                    if v is not None:
                        return v
    return default


def get_text(pages, page_list):
    return "\n".join(pages.get(p, "") for p in page_list)


def fmt(v, parens_for_neg=True):
    if v < 0 and parens_for_neg:
        return f"(${abs(v):,.0f})"
    if v < 0:
        return f"-${abs(v):,.0f}"
    return f"${v:,.0f}"


def pct(part, total):
    if total == 0:
        return 0.0
    return part / total * 100


# ─────────────────────────────────────────────────────────────
#  3. PAGE IDENTIFICATION
# ─────────────────────────────────────────────────────────────

def identify_pages(pages):
    fp = {
        "1040_p1": [], "1040_p2": [],
        "qdw": [], "form8960": [],
        "2210_p1": [], "2210_p2": [], "2210_ai": [],
        "form1116": [], "sched3": [],
    }
    for pnum, text in pages.items():
        tl = text.lower()

        if re.search(r"(form 1040|morf 1040)", tl) and \
           ("ordinary dividends" in tl or "ira distributions" in tl):
            fp["1040_p1"].append(pnum)

        if "form 1040" in tl and "page 2" in tl and "total tax" in tl:
            fp["1040_p2"].append(pnum)

        if re.search(r"form 1040 qualified dividends and capital gain tax worksheet", tl) and \
           "keep for your records" in tl:
            fp["qdw"].append(pnum)

        if re.search(r"^8960\b|form\s+8960", text, re.MULTILINE) and \
           re.search(r"part i.*investment income|investment income.*part i", tl):
            fp["form8960"].append(pnum)

        if "form 2210" in tl and "required annual payment" in tl and "part i" in tl:
            fp["2210_p1"].append(pnum)

        if "form 2210" in tl and "penalty computation" in tl and "required installments" in tl:
            fp["2210_p2"].append(pnum)

        if "schedule ai" in tl and "annualized income installment method" in tl and \
           "applicable percentage" in tl:
            fp["2210_ai"].append(pnum)

        # Form 1116: foreign tax credit
        if re.search(r"form\s+1116|^1116\b", text, re.MULTILINE) and \
           "foreign tax credit" in tl and "figuring the credit" in tl:
            fp["form1116"].append(pnum)

        # Schedule 3: additional credits
        if "schedule 3" in tl and "additional credits and payments" in tl:
            fp["sched3"].append(pnum)

    return fp


# ─────────────────────────────────────────────────────────────
#  4. EXTRACT EACH FORM
# ─────────────────────────────────────────────────────────────

def extract_1040(pages, fp):
    p1_text = get_text(pages, fp["1040_p1"]) if fp["1040_p1"] else \
              "\n".join(pages.values())

    # Filing status
    fs = "Married Filing Jointly"
    for line in p1_text.split("\n"):
        ll = line.lower()
        if "married filing jointly" in ll:
            fs = "Married Filing Jointly"; break
        if "married filing separately" in ll and re.search(r"\bx\b|☒", line, re.IGNORECASE):
            fs = "Married Filing Separately"; break
        if re.search(r"\bsingle\b", ll) and re.search(r"\bx\b|☒", line, re.IGNORECASE):
            fs = "Single"; break
        if "head of household" in ll and re.search(r"\bx\b|☒", line, re.IGNORECASE):
            fs = "Head of Household"; break
        if "qualifying surviving spouse" in ll and re.search(r"\bx\b|☒", line, re.IGNORECASE):
            fs = "Qualifying Surviving Spouse"; break

    # Tax year — accept any plausible year; MIN_YEAR/MAX_YEAR validation happens later
    tax_year = None
    for text in pages.values():
        m = re.search(r"Form 1040.*?(20\d{2})", text) or \
            re.search(r"(20\d{2})\s*U\.S\. Individual", text)
        if m:
            yr = int(m.group(1))
            if 2000 <= yr <= 2099:
                tax_year = yr; break
    if tax_year is None:
        tax_year = 0  # will be caught by validation

    # Taxpayer names from 1040 p1
    names = ""
    for line in p1_text.split("\n"):
        if re.search(r"HOWARD|NANCY|CHENG|TA\b", line, re.IGNORECASE):
            # look for combined name lines
            if len(line.strip()) > 4 and "&" in line or re.search(r"\bAND\b", line, re.IGNORECASE):
                names = line.strip()
                break
    # fallback: look for "Name(s) shown" nearby text
    if not names:
        for line in p1_text.split("\n"):
            m = re.search(r"(HOWARD|NANCY).+", line, re.IGNORECASE)
            if m:
                names = line.strip()
                break

    d = {"filing_status": fs, "tax_year": tax_year, "taxpayer_names": names}

    def inline_pair(text, left_label, right_label):
        for line in text.split("\n"):
            ml = re.search(left_label + r"\s+([\d,]+)\.", line)
            mr = re.search(right_label + r"\s+([\d,]+)\.\s*$", line)
            if ml and mr:
                return parse_number(ml.group(1)), parse_number(mr.group(1))
        return 0.0, 0.0

    d["qual_div"], d["ord_div"] = inline_pair(p1_text, r"3a", r"3b")
    if d["ord_div"] == 0.0:
        d["ord_div"] = find_line_value(p1_text,
            r"3b\b.*[Oo]rdinary dividends|[Oo]rdinary dividends.*\b3b\b")
    if d["qual_div"] == 0.0:
        d["qual_div"] = find_line_value(p1_text, r"3a\b.*[Qq]ualified dividends")

    d["ira_total"], d["ira_taxable"] = inline_pair(p1_text, r"4a", r"4b")
    if d["ira_total"] == 0.0:
        d["ira_total"]   = find_line_value(p1_text,
            r"4a\b.*IRA distributions|IRA distributions.*\b4a\b")
        d["ira_taxable"] = find_line_value(p1_text, r"\b4b\b.*[Tt]axable amount")

    d["pension_taxable"] = find_line_value(p1_text, r"\b5b\b.*[Tt]axable amount")
    d["total_income"]    = find_line_value(p1_text, r"\b9\b.*total income|total income.*\b9\b")
    d["agi"]             = find_line_value(p1_text, r"adjusted gross income|11a\b")

    p2_text = get_text(pages, fp["1040_p2"]) if fp["1040_p2"] else p1_text
    d["taxable_income"] = find_line_value(p2_text,
        r"\b15\b.*taxable income|taxable income.*\b15\b")
    if d["taxable_income"] == 0.0:
        d["taxable_income"] = find_line_value(p1_text,
            r"\b15\b.*taxable income|taxable income.*\b15\b")
    d["line16_tax"]     = find_line_value(p2_text, r"\b16\b.*Tax \(see|Tax \(see.*\b16\b")
    d["line17_sched2"]  = find_line_value(p2_text,
        r"\b17\b.*Schedule 2|Schedule 2.*\b17\b", default=0.0)
    d["line20_sched3"]  = find_line_value(p2_text,
        r"\b20\b.*Schedule 3|Schedule 3.*\b20\b", default=0.0)
    d["line22"]         = find_line_value(p2_text, r"\b22\b.*Subtract line 21")
    d["other_taxes"]    = find_line_value(p2_text, r"\b23\b.*[Oo]ther taxes", default=0.0)
    d["total_tax"]      = find_line_value(p2_text, r"\b24\b.*total tax|total tax.*\b24\b")
    d["withholding"]    = find_line_value(p2_text, r"\b25d\b|Add lines 25a through 25c")
    d["est_payments"]   = find_line_value(p2_text, r"\b26\b.*estimated tax payments")
    d["total_payments"] = find_line_value(p2_text,
        r"\b33\b.*total payments|total payments.*\b33\b")

    refund  = find_line_value(p2_text, r"\b35a\b.*refunded", default=0.0)
    balance = find_line_value(p2_text, r"\b37\b.*amount you owe", default=0.0)
    d["balance_due"] = -refund if (refund > 0 and balance == 0.0) else balance

    return d


def extract_qdw(pages, fp):
    qdw_pages = fp["qdw"]
    if not qdw_pages:
        for pnum, ptext in pages.items():
            if re.search(r"form 1040 qualified dividends and capital gain tax worksheet",
                         ptext, re.IGNORECASE):
                qdw_pages.append(pnum)
    text = pages.get(qdw_pages[0], "") if qdw_pages else ""

    def qdw_val(line_no):
        pat = r"\b" + str(line_no) + r"\s+([\d,]+(?:\.\d+)?)\.?$"
        for line in text.split("\n"):
            m = re.search(pat, line)
            if m:
                v = parse_number(m.group(1))
                if v is not None and v != float(line_no):
                    return v
        return 0.0

    return {f"qdw_line{n}": qdw_val(n) for n in range(1, 26)}


def extract_8960(pages, fp):
    niit_pages = fp["form8960"]
    if not niit_pages:
        for pnum, ptext in pages.items():
            if re.search(r"^8960\b", ptext, re.MULTILINE) and \
               re.search(r"part i.*investment income", ptext, re.IGNORECASE):
                niit_pages.append(pnum)
    text = pages.get(niit_pages[0], "") if niit_pages else ""

    def f8960_val(line_no):
        pat = r"\b" + str(line_no) + r"\s+([\d,]+(?:\.\d+)?)\.?$"
        for line in text.split("\n"):
            m = re.search(pat, line)
            if m:
                v = parse_number(m.group(1))
                if v is not None and v != float(line_no):
                    return v
        return 0.0

    d = {
        "niit_line8":    f8960_val(8),
        "niit_line12":   f8960_val(12),
        "niit_magi":     f8960_val(13),
        "niit_threshold_line": f8960_val(14),
        "niit_excess":   f8960_val(15),
        "niit_subject":  f8960_val(16),
        "niit":          f8960_val(17),
    }
    d["niit_threshold"] = d.get("niit_threshold_line", 250000.0)
    return d


def extract_foreign_tax(pages, fp):
    """Extract foreign tax credit from Form 1116 / Schedule 3."""
    # Schedule 3 line 1 = foreign tax credit
    s3_pages = fp.get("sched3", [])
    s3_text = get_text(pages, s3_pages) if s3_pages else ""
    ftc_sched3 = find_line_value(s3_text,
        r"\b1\b.*[Ff]oreign tax credit|[Ff]oreign tax credit.*\b1\b", default=0.0)

    # Form 1116 page 2 line 35 (the actual credit)
    f1116_pages = fp.get("form1116", [])
    ftc_1116 = 0.0
    # Take the first Form 1116 (non-AMT copy)
    for pnum in f1116_pages:
        text = pages.get(pnum, "")
        # Skip AMT pages (they have "Alt Min Tax" in header)
        if "alt min tax" in text.lower():
            continue
        v = find_line_value(text,
            r"\b35\b.*foreign tax credit|foreign tax credit.*\b35\b", default=0.0)
        if v > 0:
            ftc_1116 = v
            break

    ftc = ftc_sched3 if ftc_sched3 > 0 else ftc_1116
    return {"foreign_tax_credit": ftc, "ftc_sched3": ftc_sched3, "ftc_1116": ftc_1116}


def extract_2210(pages, fp, tax_year=2025):
    """
    Extract Form 2210 Schedule AI data.
    Period labels per 2210AI: 1/1–3/31, 1/1–5/31, 1/1–8/31, 1/1–12/31
    Deadlines per Form 2210 Part III column headers: 4/15, 6/15, 9/15, 1/15
    """
    p2_text = get_text(pages, fp["2210_p2"]) if fp["2210_p2"] else ""

    ny = tax_year + 1
    deadlines = [
        f"4/15/{tax_year}", f"6/15/{tax_year}",
        f"9/15/{tax_year}", f"1/15/{ny}"
    ]
    # Per spec: period start/end dates for 2210AI
    period_labels = [
        f"1/1/{tax_year}\u20133/31/{tax_year}",
        f"4/1/{tax_year}\u20135/31/{tax_year}",
        f"6/1/{tax_year}\u20138/31/{tax_year}",
        f"9/1/{tax_year}\u201312/31/{tax_year}",
    ]

    def find_4col_values(text, value_line_re):
        lines = text.split("\n")
        for line in lines:
            if re.search(value_line_re, line, re.IGNORECASE):
                nums = re.findall(r"\([\d,]+(?:\.\d+)?\)|[\d,]+(?:\.\d+)?", line)
                vals = []
                for n in nums:
                    bare = n.replace("(", "").replace(")", "").replace(",", "")
                    if len(bare) >= 1:
                        v = parse_number(n)
                        if v is not None:
                            vals.append(v)
                if len(vals) >= 4:
                    return vals[-4:]
        return [0.0, 0.0, 0.0, 0.0]

    # Line 10: Required installments
    required = find_4col_values(p2_text,
        r"fiscal year filers.*see instructions\s+10\b|see instructions\s+10\s+[\d,]")
    # Line 11: Estimated tax paid
    paid = find_4col_values(p2_text,
        r"checked a box in Part II\s+11\b|Don.t\s+file Form 2210.*\s+11\s+[\d,]")

    return {
        "ai_deadline":     deadlines,
        "ai_period_label": period_labels,
        "ai_required":     required,
        "ai_paid":         paid,
    }


# ─────────────────────────────────────────────────────────────
#  5. MASTER EXTRACTION
# ─────────────────────────────────────────────────────────────

def extract_tax_data(pages):
    fp = identify_pages(pages)
    vprint(f"      Form pages: { {k: v for k, v in fp.items() if v} }")

    data = {}
    data.update(extract_1040(pages, fp))

    # Validate tax year before doing any further work
    tax_year = data.get("tax_year", 0)
    if tax_year < MIN_YEAR or tax_year > MAX_YEAR:
        print(f"ERROR: {tax_year} is not within script's capability")
        sys.exit(1)

    data.update(extract_qdw(pages, fp))
    data.update(extract_8960(pages, fp))
    data.update(extract_foreign_tax(pages, fp))
    data.update(extract_2210(pages, fp, tax_year=data.get("tax_year", 2025)))

    # NIIT threshold from filing status
    data["niit_threshold"] = {
        "Married Filing Jointly": 250000, "Single": 200000,
        "Married Filing Separately": 125000, "Head of Household": 200000,
        "Qualifying Surviving Spouse": 250000,
    }.get(data.get("filing_status", "Married Filing Jointly"), 250000)

    if data.get("qual_div", 0) == 0 and data.get("qdw_line2", 0) != 0:
        data["qual_div"] = data["qdw_line2"]

    if data.get("qdw_line25", 0) == 0 and data.get("line16_tax", 0) != 0:
        data["qdw_line25"] = data["line16_tax"]

    return data


# ─────────────────────────────────────────────────────────────
#  6. TAX BRACKETS
# ─────────────────────────────────────────────────────────────

_HARDCODED_BRACKETS = {
    2020: {
        "Single": [
            {"rate": 0.10, "min": 0,      "max": 9875,         "label": "10%"},
            {"rate": 0.12, "min": 9876,   "max": 40125,        "label": "12%"},
            {"rate": 0.22, "min": 40126,  "max": 85525,        "label": "22%"},
            {"rate": 0.24, "min": 85526,  "max": 163300,       "label": "24%"},
            {"rate": 0.32, "min": 163301, "max": 207350,       "label": "32%"},
            {"rate": 0.35, "min": 207351, "max": 518400,       "label": "35%"},
            {"rate": 0.37, "min": 518401, "max": float("inf"), "label": "37%"},
        ],
        "Married Filing Jointly": [
            {"rate": 0.10, "min": 0,      "max": 19750,        "label": "10%"},
            {"rate": 0.12, "min": 19751,  "max": 80250,        "label": "12%"},
            {"rate": 0.22, "min": 80251,  "max": 171050,       "label": "22%"},
            {"rate": 0.24, "min": 171051, "max": 326600,       "label": "24%"},
            {"rate": 0.32, "min": 326601, "max": 414700,       "label": "32%"},
            {"rate": 0.35, "min": 414701, "max": 622050,       "label": "35%"},
            {"rate": 0.37, "min": 622051, "max": float("inf"), "label": "37%"},
        ],
        "Married Filing Separately": [
            {"rate": 0.10, "min": 0,      "max": 9875,         "label": "10%"},
            {"rate": 0.12, "min": 9875,   "max": 40125,        "label": "12%"},
            {"rate": 0.22, "min": 40125,  "max": 85525,        "label": "22%"},
            {"rate": 0.24, "min": 85525,  "max": 163300,       "label": "24%"},
            {"rate": 0.32, "min": 163300, "max": 207350,       "label": "32%"},
            {"rate": 0.35, "min": 207350, "max": 311025,       "label": "35%"},
            {"rate": 0.37, "min": 311025, "max": float("inf"), "label": "37%"},
        ],
        "Head of Household": [
            {"rate": 0.10, "min": 0,      "max": 14100,        "label": "10%"},
            {"rate": 0.12, "min": 14101,  "max": 53700,        "label": "12%"},
            {"rate": 0.22, "min": 53701,  "max": 85500,        "label": "22%"},
            {"rate": 0.24, "min": 85501,  "max": 163300,       "label": "24%"},
            {"rate": 0.32, "min": 163301, "max": 207350,       "label": "32%"},
            {"rate": 0.35, "min": 207351, "max": 518400,       "label": "35%"},
            {"rate": 0.37, "min": 518401, "max": float("inf"), "label": "37%"},
        ],
    },
    2021: {
        "Single": [
            {"rate": 0.10, "min": 0,      "max": 9950,         "label": "10%"},
            {"rate": 0.12, "min": 9951,   "max": 40525,        "label": "12%"},
            {"rate": 0.22, "min": 40526,  "max": 86375,        "label": "22%"},
            {"rate": 0.24, "min": 86376,  "max": 164925,       "label": "24%"},
            {"rate": 0.32, "min": 164926, "max": 209425,       "label": "32%"},
            {"rate": 0.35, "min": 209426, "max": 523600,       "label": "35%"},
            {"rate": 0.37, "min": 523601, "max": float("inf"), "label": "37%"},
        ],
        "Married Filing Jointly": [
            {"rate": 0.10, "min": 0,      "max": 19900,        "label": "10%"},
            {"rate": 0.12, "min": 19901,  "max": 81050,        "label": "12%"},
            {"rate": 0.22, "min": 81051,  "max": 172750,       "label": "22%"},
            {"rate": 0.24, "min": 172751, "max": 329850,       "label": "24%"},
            {"rate": 0.32, "min": 329851, "max": 418850,       "label": "32%"},
            {"rate": 0.35, "min": 418851, "max": 628300,       "label": "35%"},
            {"rate": 0.37, "min": 628301, "max": float("inf"), "label": "37%"},
        ],
        "Married Filing Separately": [
            {"rate": 0.10, "min": 0,      "max": 9950,         "label": "10%"},
            {"rate": 0.12, "min": 9950,   "max": 40525,        "label": "12%"},
            {"rate": 0.22, "min": 40525,  "max": 86375,        "label": "22%"},
            {"rate": 0.24, "min": 86375,  "max": 164925,       "label": "24%"},
            {"rate": 0.32, "min": 164925, "max": 209425,       "label": "32%"},
            {"rate": 0.35, "min": 209425, "max": 314150,       "label": "35%"},
            {"rate": 0.37, "min": 314150, "max": float("inf"), "label": "37%"},
        ],
        "Head of Household": [
            {"rate": 0.10, "min": 0,      "max": 14200,        "label": "10%"},
            {"rate": 0.12, "min": 14201,  "max": 54200,        "label": "12%"},
            {"rate": 0.22, "min": 54201,  "max": 86350,        "label": "22%"},
            {"rate": 0.24, "min": 86351,  "max": 164900,       "label": "24%"},
            {"rate": 0.32, "min": 164901, "max": 209400,       "label": "32%"},
            {"rate": 0.35, "min": 209401, "max": 523600,       "label": "35%"},
            {"rate": 0.37, "min": 523601, "max": float("inf"), "label": "37%"},
        ],
    },
    2022: {
        "Single": [
            {"rate": 0.10, "min": 0,      "max": 10275,        "label": "10%"},
            {"rate": 0.12, "min": 10276,  "max": 41775,        "label": "12%"},
            {"rate": 0.22, "min": 41776,  "max": 89075,        "label": "22%"},
            {"rate": 0.24, "min": 89076,  "max": 170050,       "label": "24%"},
            {"rate": 0.32, "min": 170051, "max": 215950,       "label": "32%"},
            {"rate": 0.35, "min": 215951, "max": 539900,       "label": "35%"},
            {"rate": 0.37, "min": 539901, "max": float("inf"), "label": "37%"},
        ],
        "Married Filing Jointly": [
            {"rate": 0.10, "min": 0,      "max": 20550,        "label": "10%"},
            {"rate": 0.12, "min": 20551,  "max": 83550,        "label": "12%"},
            {"rate": 0.22, "min": 83551,  "max": 178150,       "label": "22%"},
            {"rate": 0.24, "min": 178151, "max": 340100,       "label": "24%"},
            {"rate": 0.32, "min": 340101, "max": 431900,       "label": "32%"},
            {"rate": 0.35, "min": 431901, "max": 647850,       "label": "35%"},
            {"rate": 0.37, "min": 647851, "max": float("inf"), "label": "37%"},
        ],
        "Married Filing Separately": [
            {"rate": 0.10, "min": 0,      "max": 10275,        "label": "10%"},
            {"rate": 0.12, "min": 10275,  "max": 41775,        "label": "12%"},
            {"rate": 0.22, "min": 41775,  "max": 89075,        "label": "22%"},
            {"rate": 0.24, "min": 89075,  "max": 170050,       "label": "24%"},
            {"rate": 0.32, "min": 170050, "max": 215950,       "label": "32%"},
            {"rate": 0.35, "min": 215950, "max": 323925,       "label": "35%"},
            {"rate": 0.37, "min": 323925, "max": float("inf"), "label": "37%"},
        ],
        "Head of Household": [
            {"rate": 0.10, "min": 0,      "max": 14650,        "label": "10%"},
            {"rate": 0.12, "min": 14651,  "max": 55900,        "label": "12%"},
            {"rate": 0.22, "min": 55901,  "max": 89050,        "label": "22%"},
            {"rate": 0.24, "min": 89051,  "max": 170050,       "label": "24%"},
            {"rate": 0.32, "min": 170051, "max": 215950,       "label": "32%"},
            {"rate": 0.35, "min": 215951, "max": 539900,       "label": "35%"},
            {"rate": 0.37, "min": 539901, "max": float("inf"), "label": "37%"},
        ],
    },
    2023: {
        "Single": [
            {"rate": 0.10, "min": 0,      "max": 11000,        "label": "10%"},
            {"rate": 0.12, "min": 11001,  "max": 44725,        "label": "12%"},
            {"rate": 0.22, "min": 44726,  "max": 95375,        "label": "22%"},
            {"rate": 0.24, "min": 95376,  "max": 182050,       "label": "24%"},
            {"rate": 0.32, "min": 182051, "max": 231250,       "label": "32%"},
            {"rate": 0.35, "min": 231251, "max": 578125,       "label": "35%"},
            {"rate": 0.37, "min": 578126, "max": float("inf"), "label": "37%"},
        ],
        "Married Filing Jointly": [
            {"rate": 0.10, "min": 0,      "max": 22000,        "label": "10%"},
            {"rate": 0.12, "min": 22001,  "max": 89450,        "label": "12%"},
            {"rate": 0.22, "min": 89451,  "max": 190750,       "label": "22%"},
            {"rate": 0.24, "min": 190751, "max": 364200,       "label": "24%"},
            {"rate": 0.32, "min": 364201, "max": 462500,       "label": "32%"},
            {"rate": 0.35, "min": 462501, "max": 693750,       "label": "35%"},
            {"rate": 0.37, "min": 693751, "max": float("inf"), "label": "37%"},
        ],
        "Married Filing Separately": [
            {"rate": 0.10, "min": 0,      "max": 11000,        "label": "10%"},
            {"rate": 0.12, "min": 11000,  "max": 44725,        "label": "12%"},
            {"rate": 0.22, "min": 44725,  "max": 95375,        "label": "22%"},
            {"rate": 0.24, "min": 95375,  "max": 182050,       "label": "24%"},
            {"rate": 0.32, "min": 182050, "max": 231250,       "label": "32%"},
            {"rate": 0.35, "min": 231250, "max": 346875,       "label": "35%"},
            {"rate": 0.37, "min": 346875, "max": float("inf"), "label": "37%"},
        ],
        "Head of Household": [
            {"rate": 0.10, "min": 0,      "max": 15700,        "label": "10%"},
            {"rate": 0.12, "min": 15701,  "max": 59850,        "label": "12%"},
            {"rate": 0.22, "min": 59851,  "max": 95350,        "label": "22%"},
            {"rate": 0.24, "min": 95351,  "max": 182050,       "label": "24%"},
            {"rate": 0.32, "min": 182051, "max": 231250,       "label": "32%"},
            {"rate": 0.35, "min": 231251, "max": 578100,       "label": "35%"},
            {"rate": 0.37, "min": 578101, "max": float("inf"), "label": "37%"},
        ],
    },
    2024: {
        "Single": [
            {"rate": 0.10, "min": 0,      "max": 11600,        "label": "10%"},
            {"rate": 0.12, "min": 11601,  "max": 47150,        "label": "12%"},
            {"rate": 0.22, "min": 47151,  "max": 100525,       "label": "22%"},
            {"rate": 0.24, "min": 100526, "max": 191950,       "label": "24%"},
            {"rate": 0.32, "min": 191951, "max": 243725,       "label": "32%"},
            {"rate": 0.35, "min": 243726, "max": 609350,       "label": "35%"},
            {"rate": 0.37, "min": 609351, "max": float("inf"), "label": "37%"},
        ],
        "Married Filing Jointly": [
            {"rate": 0.10, "min": 0,      "max": 23200,        "label": "10%"},
            {"rate": 0.12, "min": 23201,  "max": 94300,        "label": "12%"},
            {"rate": 0.22, "min": 94301,  "max": 201050,       "label": "22%"},
            {"rate": 0.24, "min": 201051, "max": 383900,       "label": "24%"},
            {"rate": 0.32, "min": 383901, "max": 487450,       "label": "32%"},
            {"rate": 0.35, "min": 487451, "max": 731200,       "label": "35%"},
            {"rate": 0.37, "min": 731201, "max": float("inf"), "label": "37%"},
        ],
        "Married Filing Separately": [
            {"rate": 0.10, "min": 0,      "max": 11600,        "label": "10%"},
            {"rate": 0.12, "min": 11600,  "max": 47150,        "label": "12%"},
            {"rate": 0.22, "min": 47150,  "max": 100525,       "label": "22%"},
            {"rate": 0.24, "min": 100525, "max": 191950,       "label": "24%"},
            {"rate": 0.32, "min": 191950, "max": 243725,       "label": "32%"},
            {"rate": 0.35, "min": 243725, "max": 365600,       "label": "35%"},
            {"rate": 0.37, "min": 365600, "max": float("inf"), "label": "37%"},
        ],
        "Head of Household": [
            {"rate": 0.10, "min": 0,      "max": 16550,        "label": "10%"},
            {"rate": 0.12, "min": 16551,  "max": 63100,        "label": "12%"},
            {"rate": 0.22, "min": 63101,  "max": 100500,       "label": "22%"},
            {"rate": 0.24, "min": 100501, "max": 191950,       "label": "24%"},
            {"rate": 0.32, "min": 191951, "max": 243700,       "label": "32%"},
            {"rate": 0.35, "min": 243701, "max": 609350,       "label": "35%"},
            {"rate": 0.37, "min": 609351, "max": float("inf"), "label": "37%"},
        ],
    },
    2025: {
        "Single": [
            {"rate": 0.10, "min": 0,      "max": 11925,        "label": "10%"},
            {"rate": 0.12, "min": 11926,  "max": 48475,        "label": "12%"},
            {"rate": 0.22, "min": 48476,  "max": 103350,       "label": "22%"},
            {"rate": 0.24, "min": 103351, "max": 197300,       "label": "24%"},
            {"rate": 0.32, "min": 197301, "max": 250525,       "label": "32%"},
            {"rate": 0.35, "min": 250526, "max": 626350,       "label": "35%"},
            {"rate": 0.37, "min": 626351, "max": float("inf"), "label": "37%"},
        ],
        "Married Filing Jointly": [
            {"rate": 0.10, "min": 0,      "max": 23850,        "label": "10%"},
            {"rate": 0.12, "min": 23851,  "max": 96950,        "label": "12%"},
            {"rate": 0.22, "min": 96951,  "max": 206700,       "label": "22%"},
            {"rate": 0.24, "min": 206701, "max": 394600,       "label": "24%"},
            {"rate": 0.32, "min": 394601, "max": 501050,       "label": "32%"},
            {"rate": 0.35, "min": 501051, "max": 751600,       "label": "35%"},
            {"rate": 0.37, "min": 751601, "max": float("inf"), "label": "37%"},
        ],
        "Married Filing Separately": [
            {"rate": 0.10, "min": 0,      "max": 11925,        "label": "10%"},
            {"rate": 0.12, "min": 11925,  "max": 48475,        "label": "12%"},
            {"rate": 0.22, "min": 48475,  "max": 103350,       "label": "22%"},
            {"rate": 0.24, "min": 103350, "max": 197300,       "label": "24%"},
            {"rate": 0.32, "min": 197300, "max": 250525,       "label": "32%"},
            {"rate": 0.35, "min": 250525, "max": 375800,       "label": "35%"},
            {"rate": 0.37, "min": 375800, "max": float("inf"), "label": "37%"},
        ],
        "Head of Household": [
            {"rate": 0.10, "min": 0,      "max": 17000,        "label": "10%"},
            {"rate": 0.12, "min": 17001,  "max": 64850,        "label": "12%"},
            {"rate": 0.22, "min": 64851,  "max": 103350,       "label": "22%"},
            {"rate": 0.24, "min": 103351, "max": 197300,       "label": "24%"},
            {"rate": 0.32, "min": 197301, "max": 250500,       "label": "32%"},
            {"rate": 0.35, "min": 250501, "max": 626350,       "label": "35%"},
            {"rate": 0.37, "min": 626351, "max": float("inf"), "label": "37%"},
        ],
    },
}

_FS_COLUMN_KEYWORDS = {
    "Married Filing Jointly":    ["married filing jointly", "mfj", "joint"],
    "Single":                    ["single"],
    "Married Filing Separately": ["married filing separately", "mfs", "separately"],
    "Head of Household":         ["head of household", "hoh"],
    "Qualifying Surviving Spouse": ["married filing jointly", "mfj", "joint"],
}


def _hardcoded_brackets(filing_status, tax_year):
    available = sorted(_HARDCODED_BRACKETS.keys())
    year = tax_year if tax_year in _HARDCODED_BRACKETS else \
           min(available, key=lambda y: abs(y - tax_year))
    yr_table = _HARDCODED_BRACKETS[year]
    fs = filing_status
    if fs == "Qualifying Surviving Spouse":
        fs = "Married Filing Jointly"
    return yr_table.get(fs, yr_table.get("Married Filing Jointly",
           list(yr_table.values())[0]))


def _parse_taxfoundation_table(soup, filing_status):
    """Parse the first matching bracket table from a BeautifulSoup object."""
    keywords = _FS_COLUMN_KEYWORDS.get(filing_status,
               _FS_COLUMN_KEYWORDS["Married Filing Jointly"])
    target_col = None
    target_table = None
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        for col_idx, hdr in enumerate(headers):
            if any(kw in hdr for kw in keywords):
                target_col = col_idx
                target_table = table
                break
        if target_table:
            break
    if not target_table or target_col is None:
        return None
    brackets = []
    for row in target_table.find_all("tr")[1:]:
        cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
        if len(cells) < 2:
            continue
        rate_match = re.search(r"(\d+)%", cells[0])
        if not rate_match:
            continue
        rate = float(rate_match.group(1)) / 100
        col = min(target_col, len(cells) - 1)
        range_text = cells[col]
        nums = re.findall(r"[\d,]+", range_text.replace("$", ""))
        nums = [int(n.replace(",", "")) for n in nums if n.replace(",", "").isdigit()]
        if len(nums) == 0:
            continue
        elif len(nums) == 1:
            lo, hi = nums[0], float("inf")
        else:
            lo, hi = nums[0], nums[1]
        brackets.append({"rate": rate, "min": lo, "max": hi,
                         "label": f"{int(rate*100)}%"})
    if len(brackets) >= 5:
        return sorted(brackets, key=lambda b: b["min"])
    return None


def _scrape_taxfoundation(filing_status, tax_year):
    """Scrape year-specific bracket table from taxfoundation.org."""
    try:
        from bs4 import BeautifulSoup
        url = f"https://taxfoundation.org/data/all/federal/{tax_year}-tax-brackets/"
        resp = requests.get(url, timeout=15,
                            headers={"User-Agent": "Mozilla/5.0 taxsum/1.0"})
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        result = _parse_taxfoundation_table(soup, filing_status)
        if result:
            vprint(f"[INFO] Brackets scraped from {url}")
            return result
    except Exception as e:
        vprint(f"[INFO] taxfoundation.org scrape failed: {e}")
    return None


def fetch_tax_brackets(filing_status, tax_year):
    if tax_year < MIN_YEAR or tax_year > MAX_YEAR:
        print(f"ERROR: {tax_year} is not within script's capability")
        sys.exit(1)

    scraped = _scrape_taxfoundation(filing_status, tax_year)
    if scraped:
        return scraped

    brackets = _hardcoded_brackets(filing_status, tax_year)
    vprint(f"[INFO] Using hardcoded {tax_year} brackets for {filing_status}.")
    return brackets


# ─────────────────────────────────────────────────────────────
#  7. BRACKET BREAKDOWN
# ─────────────────────────────────────────────────────────────

def compute_bracket_breakdown(ordinary_income, brackets, total_tax_line25):
    rows = []
    remaining = ordinary_income
    for b in brackets:
        if remaining <= 0:
            break
        width = (b["max"] - b["min"]) if b["max"] != float("inf") else remaining
        amt = min(remaining, width)
        amt = max(0.0, amt)
        tax_in = amt * b["rate"]
        rows.append({"label": b["label"], "income": amt, "tax": tax_in,
                     "pct": pct(tax_in, total_tax_line25)})
        remaining -= amt
    return rows


# ─────────────────────────────────────────────────────────────
#  8a. PIE CHART HELPER
# ─────────────────────────────────────────────────────────────

PIE_COLORS = [
    "#1f77b4",  # muted blue
    "#ff7f0e",  # safety orange
    "#2ca02c",  # cooked asparagus green
    "#d62728",  # brick red
    "#9467bd",  # muted purple
    "#8c564b",  # chestnut brown
    "#e377c2",  # raspberry pink
    "#17becf",  # blue-teal
]

def make_pie(labels, values, amounts=None, width_in=2.8, height_in=2.8):
    """
    Render a pie chart with smart inside/outside label placement.
    Each label shows: abbreviated name, percentage, and amount rounded to $k.
    Inside for slices ≥12%, outside with leader line for smaller slices.
    """
    pairs = [(l, v, (amounts[i] if amounts else v))
             for i, (l, v) in enumerate(zip(labels, values)) if v > 0]
    if not pairs:
        return None
    lbls, vals, amts = zip(*pairs)
    clrs = PIE_COLORS[:len(vals)]
    total = sum(vals)

    fig, ax = plt.subplots(figsize=(width_in, height_in))
    wedges, _ = ax.pie(
        vals,
        colors=clrs,
        startangle=90,
        wedgeprops={"linewidth": 0.6, "edgecolor": "white"},
    )
    ax.set_aspect("equal")

    for wedge, lbl, val, amt in zip(wedges, lbls, vals, amts):
        pct_val = val / total * 100
        angle   = (wedge.theta1 + wedge.theta2) / 2
        cos_a   = math.cos(math.radians(angle))
        sin_a   = math.sin(math.radians(angle))
        # Amount rounded to nearest $k
        amt_k   = f"${amt/1000:.0f}k"
        short   = f"{lbl}\n{pct_val:.1f}%\n{amt_k}"

        if pct_val >= 12:
            ax.text(0.6 * cos_a, 0.6 * sin_a, short,
                    ha="center", va="center",
                    fontsize=5.5, fontweight="bold", color="white",
                    multialignment="center")
        else:
            ha = "left" if cos_a >= 0 else "right"
            ax.annotate(
                short,
                xy=(cos_a, sin_a),
                xytext=(1.15 * cos_a, 1.15 * sin_a),
                fontsize=5, color="black",
                multialignment="center", ha=ha, va="center",
                arrowprops=dict(arrowstyle="-", color="gray", lw=0.5),
            )

    plt.tight_layout(pad=0.1)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor="white", transparent=False)
    plt.close(fig)
    buf.seek(0)
    return Image(buf, width=width_in * inch, height=height_in * inch)


def table_with_pie(table_flowable, pie_flowable, table_width=6.0, gap=0.2):
    """
    Place a table and pie chart side by side using a two-column wrapper Table.
    If pie_flowable is None, returns just the table.
    """
    if pie_flowable is None:
        return table_flowable
    wrapper = Table(
        [[table_flowable, pie_flowable]],
        colWidths=[table_width * inch, (8.5 - 1.5 - table_width - gap) * inch],
        hAlign="LEFT",
    )
    wrapper.setStyle(TableStyle([
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
    ]))
    return wrapper


# ─────────────────────────────────────────────────────────────
#  8. BUILD SUMMARY PDF
# ─────────────────────────────────────────────────────────────

def build_summary_pdf(data, brackets, out_path):
    doc = SimpleDocTemplate(out_path, pagesize=letter,
                            leftMargin=0.65*inch, rightMargin=0.65*inch,
                            topMargin=0.6*inch,  bottomMargin=0.6*inch)
    styles = getSampleStyleSheet()
    normal = styles["Normal"]

    def sty(name, parent=None, **kw):
        return ParagraphStyle(name, parent=parent or normal, **kw)

    title_s = sty("T",  fontSize=14, fontName="Helvetica-Bold", spaceAfter=2,
                  textColor=colors.HexColor("#1a3557"))
    sub_s   = sty("Su", fontSize=8,  textColor=colors.HexColor("#444444"), spaceAfter=4)
    h1      = sty("H1", fontSize=10, fontName="Helvetica-Bold",
                  textColor=colors.HexColor("#1a3557"), spaceAfter=3, spaceBefore=8)
    h2      = sty("H2", fontSize=9,  fontName="Helvetica-Bold",
                  textColor=colors.HexColor("#2e5f8a"), spaceAfter=2, spaceBefore=6)
    small   = sty("Sm", fontSize=7,  textColor=colors.HexColor("#555555"), spaceAfter=2)

    BLUE_DARK  = colors.HexColor("#1a3557")
    BLUE_MED   = colors.HexColor("#2e5f8a")
    BLUE_LIGHT = colors.HexColor("#e8f0f8")
    ALT        = colors.HexColor("#f4f8fc")

    # Usable width = 8.5 - 0.65 - 0.65 = 7.2 inches
    W = 7.2

    def mk_ts(hbg=BLUE_MED, alt=ALT):
        return TableStyle([
            ("BACKGROUND",    (0, 0),  (-1, 0),  hbg),
            ("TEXTCOLOR",     (0, 0),  (-1, 0),  colors.white),
            ("FONTNAME",      (0, 0),  (-1, 0),  "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0),  (-1, 0),  8),
            ("ALIGN",         (1, 0),  (-1, -1), "RIGHT"),
            ("ALIGN",         (0, 0),  (0, -1),  "LEFT"),
            ("FONTSIZE",      (0, 1),  (-1, -1), 8),
            ("ROWBACKGROUNDS",(0, 1),  (-1, -1), [colors.white, alt]),
            ("GRID",          (0, 0),  (-1, -1), 0.4, colors.HexColor("#aaaaaa")),
            ("TOPPADDING",    (0, 0),  (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0),  (-1, -1), 2),
            ("LEFTPADDING",   (0, 0),  (-1, -1), 5),
            ("RIGHTPADDING",  (0, 0),  (-1, -1), 5),
        ])

    PIE_W = 2.2
    PIE_H = 2.2

    def tbl_pie(tbl_flowable, pie_img, tbl_w):
        """Table on left, pie on right; falls back to table-only if no pie."""
        if pie_img is None:
            return tbl_flowable
        right_w = W - tbl_w
        wrapper = Table([[tbl_flowable, pie_img]],
                        colWidths=[tbl_w*inch, right_w*inch], hAlign="LEFT")
        wrapper.setStyle(TableStyle([
            ("VALIGN",       (0,0),(-1,-1),"TOP"),
            ("LEFTPADDING",  (0,0),(-1,-1),0),
            ("RIGHTPADDING", (0,0),(-1,-1),0),
            ("TOPPADDING",   (0,0),(-1,-1),0),
            ("BOTTOMPADDING",(0,0),(-1,-1),0),
        ]))
        return wrapper

    story = []

    def section(els):
        story.append(KeepTogether(els))

    # ── TITLE ──────────────────────────────────────────────────
    names_str = data.get("taxpayer_names", "")
    story.append(Paragraph(f"Tax Summary — {data['tax_year']}", title_s))
    if names_str:
        story.append(Paragraph(names_str, sub_s))
    story.append(Paragraph(
        f"Filing Status: {data['filing_status']}  |  "
        f"AGI: {fmt(data['agi'])}  |  "
        f"Taxable Income: {fmt(data['taxable_income'])}  |  "
        f"Total Tax: {fmt(data['total_tax'])}  |  "
        f"Effective Rate: {pct(data['total_tax'], data['agi']):.1f}%",
        sub_s))
    story.append(HRFlowable(width="100%", thickness=1.5, color=BLUE_DARK, spaceAfter=6))

    # ── INCOME BREAKDOWN ───────────────────────────────────────
    ti       = data["total_income"]
    non_qual = data["ord_div"] - data["qual_div"]
    ttq      = data["qdw_line25"]

    TBL_W = W - PIE_W - 0.1
    inc_rows = [
        ["Income Category", "Amount", "% of Total Income"],
        ["Ordinary Dividends minus Qualified Dividends (3b − 3a)",
         fmt(non_qual),         f"{pct(non_qual, ti):.1f}%"],
        ["Qualified Dividends (line 3a)",
         fmt(data["qual_div"]), f"{pct(data['qual_div'], ti):.1f}%"],
        ["IRA Withdrawal — Taxable (line 4b)",
         fmt(data["ira_taxable"]), f"{pct(data['ira_taxable'], ti):.1f}%"],
        ["Total Income (line 9)", fmt(ti), "100.0%"],
    ]
    inc_t = Table(inc_rows, colWidths=[2.85*inch, 1.0*inch, 1.05*inch])
    its = mk_ts(); its.add("FONTNAME",(0,-1),(-1,-1),"Helvetica-Bold")
    its.add("BACKGROUND",(0,-1),(-1,-1),BLUE_LIGHT); inc_t.setStyle(its)
    inc_pie = make_pie(
        ["Ord−Qual\nDiv", "Qual\nDiv", "IRA"],
        [non_qual, data["qual_div"], data["ira_taxable"]],
        amounts=[non_qual, data["qual_div"], data["ira_taxable"]],
        width_in=PIE_W, height_in=PIE_H)
    section([
        Paragraph("Income Breakdown", h1),
        tbl_pie(inc_t, inc_pie, TBL_W),
        Spacer(1, 4),
    ])

    # ── TAX BREAKDOWN (QDW) ────────────────────────────────────
    tax_rows = [
        ["Category", "Income Amount", "Tax Amount", "% of Total Tax"],
        ["15% Bracket (line 17 / line 18)",
         fmt(data["qdw_line17"]), fmt(data["qdw_line18"]),
         f"{pct(data['qdw_line18'], ttq):.1f}%"],
        ["20% Bracket (line 20 / line 21)",
         fmt(data["qdw_line20"]), fmt(data["qdw_line21"]),
         f"{pct(data['qdw_line21'], ttq):.1f}%"],
        ["Ordinary Income (line 5 / line 22)",
         fmt(data["qdw_line5"]), fmt(data["qdw_line22"]),
         f"{pct(data['qdw_line22'], ttq):.1f}%"],
        ["Total Tax (line 25)", "", fmt(ttq), "100.0%"],
    ]
    tts = mk_ts(); tts.add("FONTNAME",(0,-1),(-1,-1),"Helvetica-Bold")
    tts.add("BACKGROUND",(0,-1),(-1,-1),BLUE_LIGHT)
    tax_t = Table(tax_rows, colWidths=[2.2*inch, 1.0*inch, 0.9*inch, 0.8*inch])
    tax_t.setStyle(tts)
    tax_pie = make_pie(
        ["15%", "20%", "Ordinary"],
        [data["qdw_line17"], data["qdw_line20"], data["qdw_line5"]],
        amounts=[data["qdw_line17"], data["qdw_line20"], data["qdw_line5"]],
        width_in=PIE_W, height_in=PIE_H)
    section([
        Paragraph("Tax Breakdown — Qualified Dividends &amp; Capital Gain Tax Worksheet", h1),
        Paragraph("All line references from the QD &amp; Capital Gain Tax Worksheet. Total Tax = line 25.", small),
        Spacer(1, 2),
        tbl_pie(tax_t, tax_pie, TBL_W),
        Spacer(1, 4),
    ])

    # ── ORDINARY BRACKET BREAKDOWN ─────────────────────────────
    ord_bd = compute_bracket_breakdown(data["qdw_line5"], brackets, ttq)
    ord_rows = [["Bracket", "Income in Bracket", "Tax in Bracket", "% of Total Tax"]]
    for b in ord_bd:
        if b["income"] > 0:
            ord_rows.append([b["label"], fmt(b["income"]), fmt(b["tax"]),
                             f"{b['pct']:.1f}%"])
    ord_rows.append(["Total", fmt(data["qdw_line5"]), fmt(data["qdw_line22"]),
                     f"{pct(data['qdw_line22'], ttq):.1f}%"])
    ots = mk_ts(hbg=BLUE_MED)
    ots.add("FONTNAME",(0,-1),(-1,-1),"Helvetica-Bold")
    ots.add("BACKGROUND",(0,-1),(-1,-1),BLUE_LIGHT)
    ord_t = Table(ord_rows, colWidths=[0.6*inch, 1.75*inch, 1.5*inch, 1.05*inch])
    ord_t.setStyle(ots)
    ord_pie = make_pie(
        [b["label"] for b in ord_bd if b["income"] > 0],
        [b["income"] for b in ord_bd if b["income"] > 0],
        amounts=[b["income"] for b in ord_bd if b["income"] > 0],
        width_in=PIE_W, height_in=PIE_H)
    section([
        Paragraph("Ordinary Income Tax Bracket Breakdown", h2),
        Paragraph(
            f"Ordinary taxable income {fmt(data['qdw_line5'])} across "
            f"{data['tax_year']} {data['filing_status']} brackets:", small),
        Spacer(1, 2),
        tbl_pie(ord_t, ord_pie, TBL_W),
        Spacer(1, 4),
    ])

    # ── NIIT ───────────────────────────────────────────────────
    niit_rows = [
        ["Item", "Amount"],
        ["NIIT @ 3.8% (Form 8960 line 17)", fmt(data["niit"])],
    ]
    niit_t = Table(niit_rows, colWidths=[5.4*inch, 1.8*inch])
    niit_t.setStyle(mk_ts())
    section([Paragraph("Net Investment Income Tax (NIIT)", h1),
             niit_t, Spacer(1, 4)])

    # ── FOREIGN TAX CREDIT ─────────────────────────────────────
    ftc_rows = [
        ["Item", "Amount"],
        ["Foreign Tax Credit (Schedule 3 line 1 / Form 1116 line 35)",
         fmt(data.get("foreign_tax_credit", 0))],
    ]
    ftc_t = Table(ftc_rows, colWidths=[5.4*inch, 1.8*inch])
    ftc_t.setStyle(mk_ts())
    section([Paragraph("Foreign Tax Credit", h1),
             ftc_t, Spacer(1, 4)])

    # ── PAYMENT SCHEDULE ───────────────────────────────────────
    pay_rows = [
        ["", "Period 1", "Period 2", "Period 3", "Period 4"],
        ["Period"] + data["ai_period_label"],
        ["Deadline"] + data["ai_deadline"],
        ["Required Payment"] + [fmt(v) for v in data["ai_required"]],
        ["Estimated Tax Paid"] + [fmt(v) for v in data["ai_paid"]],
    ]
    pts = mk_ts()
    pts.add("FONTNAME",(0,0),(-1,1),"Helvetica-Bold")
    pts.add("BACKGROUND",(0,0),(-1,1),BLUE_DARK)
    pts.add("TEXTCOLOR",(0,0),(-1,1),colors.white)
    pay_t = Table(pay_rows,
                  colWidths=[1.4*inch, 1.45*inch, 1.45*inch, 1.45*inch, 1.45*inch])
    pay_t.setStyle(pts)
    section([
        Paragraph("Tax Payment Schedule — Form 2210 / Schedule AI", h1),
        Paragraph("Period dates: 1/1–3/31 · 4/1–5/31 · 6/1–8/31 · 9/1–12/31. "
                  "Deadlines per Form 2210 Part III. ES vouchers for next year excluded.", small),
        Spacer(1, 2),
        pay_t, Spacer(1, 4),
    ])

    # ── BALANCE DUE / REFUND ───────────────────────────────────
    bal = data["balance_due"]
    bal_label = "Refund" if bal < 0 else "Balance Due"
    bal_rows = [["Item", "Amount"], [bal_label, fmt(abs(bal))]]
    bts = mk_ts()
    bts.add("FONTNAME",(0,-1),(-1,-1),"Helvetica-Bold")
    bts.add("BACKGROUND",(0,-1),(-1,-1),
            colors.HexColor("#ffe0b2") if bal >= 0 else colors.HexColor("#c8e6c9"))
    bal_t = Table(bal_rows, colWidths=[5.4*inch, 1.8*inch])
    bal_t.setStyle(bts)
    section([Paragraph("Remaining Tax Owed / Refund", h1), bal_t])

    doc.build(story)
    vprint(f"[OK] Summary written to: {out_path}")


# ─────────────────────────────────────────────────────────────
#  9. DEBUG
# ─────────────────────────────────────────────────────────────

def print_debug(pages, data):
    print("\n" + "="*60)
    print("DEBUG: EXTRACTED VALUES")
    print("="*60)
    for k, v in sorted(data.items()):
        print(f"  {k:<38} = {v}")
    print("\n" + "="*60)
    print("DEBUG: KEY PAGE TEXT SAMPLES")
    print("="*60)
    for pnum in sorted(pages.keys()):
        txt = pages[pnum]
        if any(kw in txt.lower() for kw in [
            "form 1040", "ordinary dividends", "qualified div",
            "schedule ai", "net investment", "form 8960", "form 2210",
            "penalty computation", "foreign tax credit", "1116"
        ]):
            print(f"\n--- Page {pnum} (first 800 chars) ---")
            print(txt[:800])


# ─────────────────────────────────────────────────────────────
#  10. MAIN
# ─────────────────────────────────────────────────────────────

def main():
    global _verbose
    debug = "--debug" in sys.argv
    _verbose = debug
    args  = [a for a in sys.argv[1:] if not a.startswith("--")]

    if not args:
        print("Usage: python taxsum.py taxin.pdf [--debug]")
        sys.exit(1)

    in_pdf = args[0]
    if not os.path.isfile(in_pdf):
        print(f"Error: file not found: {in_pdf}"); sys.exit(1)

    base    = os.path.splitext(in_pdf)[0]
    out_pdf = base + "_taxsum.pdf"
    try:
        tf = base + "_write_test_"; open(tf, "w").close(); os.remove(tf)
    except OSError:
        sd = os.path.dirname(os.path.abspath(__file__))
        out_pdf = os.path.join(sd, os.path.basename(base) + "_taxsum.pdf")

    pages = extract_all_text(in_pdf)
    data  = extract_tax_data(pages)

    vprint(f"      Year: {data['tax_year']}  Status: {data['filing_status']}")
    vprint(f"      AGI: {fmt(data.get('agi',0))}  "
           f"Taxable: {fmt(data.get('taxable_income',0))}  "
           f"Total Tax: {fmt(data.get('total_tax',0))}")

    if debug:
        print_debug(pages, data)

    brackets = fetch_tax_brackets(data["filing_status"], data["tax_year"])
    build_summary_pdf(data, brackets, out_pdf)


if __name__ == "__main__":
    main()
