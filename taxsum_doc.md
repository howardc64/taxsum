# taxsum.py — Tax Summary Generator

## Overview

`taxsum.py` reads a TurboTax-generated federal tax return PDF and produces a concise one-page summary PDF (`taxin_taxsum.pdf`) covering income breakdown, tax breakdown, bracket analysis, NIIT, foreign tax credit, estimated payment schedule, and the final balance due or refund.

---

## Usage

```
python taxsum.py  taxin.pdf [--debug]
```

| Argument | Description |
|---|---|
| `taxin.pdf` | Input federal tax return PDF (TurboTax format) |
| `--debug` | Print progress messages and all extracted values to stdout |

**Output file:** `taxin_taxsum.pdf` written to the same directory as the input. If that directory is read-only, the output is written to the script's directory instead.

**Default behavior is silent** — no console output unless `--debug` is passed or a fatal error occurs.

---

## Supported Tax Years

`MIN_YEAR = 2020`, `MAX_YEAR = 2025`

If the tax year found in the PDF is outside this range, the script prints:

```
ERROR: <year> is not within script's capability
```

and exits immediately. The check runs as soon as the year is read from the 1040, before any further processing.

---

## Dependencies

```
pip install pdfplumber reportlab requests beautifulsoup4
```

| Package | Purpose |
|---|---|
| `pdfplumber` | PDF text extraction |
| `reportlab` | PDF generation |
| `requests` | Tax bracket web scraping |
| `beautifulsoup4` | HTML parsing for bracket scrape |

---

## Output PDF Sections

Each section is wrapped in `KeepTogether` so it will not split across pages.

### 1. Header
Taxpayer name, filing status, AGI, taxable income, total tax, and effective tax rate.

### 2. Income Breakdown
Percentage of total income (Form 1040 line 9) for three categories:

| Row | Source |
|---|---|
| Ordinary Dividends minus Qualified Dividends | Line 3b minus line 3a |
| Qualified Dividends | Line 3a |
| IRA Withdrawal — Taxable | Line 4b |

### 3. Tax Breakdown
From the **Qualified Dividends and Capital Gain Tax Worksheet** (QDW). Total tax = line 25.

| Row | Income | Tax | % of Total |
|---|---|---|---|
| 15% Tax Bracket | Line 17 | Line 18 | Line 18 ÷ 25 |
| 20% Tax Bracket | Line 20 | Line 21 | Line 21 ÷ 25 |
| Ordinary Income | Line 5 | Line 22 | Line 22 ÷ 25 |

### 4. Ordinary Income Bracket Breakdown
Distributes QDW line 5 (ordinary taxable income) across the year's federal brackets for the taxpayer's filing status. For each bracket shows income within the bracket, tax, and percentage of total tax (QDW line 25).

### 5. Net Investment Income Tax (NIIT)
Single line: NIIT @ 3.8% from Form 8960 line 17.

### 6. Foreign Tax Credit
Single line: credit from Schedule 3 line 1 (or Form 1116 line 35 if Schedule 3 is unavailable).

### 7. Tax Payment Schedule
From Form 2210 Part III / Schedule AI. ES vouchers for next year are excluded.

| Row | Content |
|---|---|
| Period | 1/1–3/31 · 4/1–5/31 · 6/1–8/31 · 9/1–12/31 |
| Deadline | 4/15 · 6/15 · 9/15 · 1/15 (next year) |
| Required Payment | Form 2210 Part III line 10 (4 columns) |
| Estimated Tax Paid | Form 2210 Part III line 11 (4 columns) |

### 8. Remaining Tax Owed / Refund
Single line showing either **Balance Due** or **Refund** amount.

---

## Tax Bracket Data

Brackets are sourced in this order:

1. **Live scrape** from `https://taxfoundation.org/data/all/federal/<year>-tax-brackets/`
2. **Hardcoded fallback** tables built into the script for 2020–2025

Filing statuses covered: Single, Married Filing Jointly, Married Filing Separately, Head of Household, Qualifying Surviving Spouse (treated as Married Filing Jointly for bracket purposes).

---

## Forms Read

| Form / Worksheet | Data Extracted |
|---|---|
| Form 1040 page 1 | Filing status, tax year, names, dividends (3a/3b), IRA distributions (4a/4b), total income (line 9), AGI (line 11a) |
| Form 1040 page 2 | Taxable income (15), tax (16), credits (20), total tax (24), withholding (25d), estimated payments (26), total payments (33), balance/refund (37/35a) |
| QD & Capital Gain Tax Worksheet | Lines 1–25 |
| Form 8960 | Line 17 (NIIT) |
| Schedule 3 | Line 1 (Foreign Tax Credit) |
| Form 1116 | Line 35 (Foreign Tax Credit, non-AMT copy) |
| Form 2210 Part III | Lines 10–11 (4 payment periods) |

---

## Debug Mode

```
python taxsum.py taxin.pdf --debug
```

Prints to stdout:
- Pages identified for each form
- All extracted numeric values (alphabetically)
- First 800 characters of each tax-relevant page

---

## Extending the Year Range

To add support for a new tax year:

1. Add the bracket table for that year to `_HARDCODED_BRACKETS` in the script following the existing pattern.
2. Update `MAX_YEAR` at the top of the script.

To change the minimum year, update `MIN_YEAR`. No other changes are needed — the year validation, scrape range, and fallback logic all reference these two constants.

---

## Known Limitations

- Designed for **TurboTax-generated PDFs**. Other tax software may use different page layouts or label text, which can cause extraction to return zero for some fields.
- The annualized income installment method (Schedule AI) is assumed. Returns using the regular underpayment method (equal quarterly installments) will show the correct required payments but the period labels will reflect AI dates.
- Pension/annuity income (line 5b) is extracted but not displayed in the income breakdown table.
