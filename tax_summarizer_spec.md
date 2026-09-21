# Tax Return Summarizer — Summary & Specification

**What it is:** a single self-contained HTML file (`index.html`, named for direct GitHub Pages hosting) that turns a filed federal tax return PDF (Form 1040 or 1040-SR, TurboTax e-file style) into a one-page printable summary — income mix, capital-gains tax layers, ordinary bracket breakdown, deductions, and payment schedule — with everything computed and rendered **entirely client-side in the browser**. No server, no upload, no external API calls. The PDF never leaves the person's machine.

Tax years supported: **2023–2026**. Filing statuses: Single, MFJ, MFS, HOH, QSS.

---

## 1. How it works, end to end

1. **Upload** — person drops a PDF onto the intake screen.
2. **Extract** — `pdf.js` (loaded from a pinned CDN version) pulls raw text items with x/y coordinates from every page, which are re-sorted into reading order (top-to-bottom, left-to-right) to reconstruct plain text lines. This is necessary because PDF text streams don't preserve visual layout order.
3. **Parse** — `parseReturn(text)` runs a declarative field schema plus a handful of bespoke parsers (see §2) against the reconstructed text to pull out every number the report needs.
4. **Confirm** — the person sees the detected tax year and filing status (editable, in case detection is wrong) and sets an **income-breakdown cutoff %** (default 5%).
5. **Validate** — `validateReturn(r)` cross-checks the parsed numbers against the return's own arithmetic (§3) and attaches any mismatches as warnings.
6. **Render** — `buildReport(r, status, cutoff)` produces the report HTML: income breakdown, tax breakdown, deductions, NIIT, foreign tax credit, estimated-payment schedule, refund/owed — each as its own section with a table and, where relevant, a pie chart drawn as inline SVG.
7. **Print / Save** — see §6.

---

## 2. Parsing architecture

### 2.1 Field schema

Most fields fit one shape: *find an anchor phrase, then find a number near it*. These ~30 fields live in one declarative table, `FIELD_SCHEMA`:

```js
{ key: 'wages', scope: 'full', anchor: 'Add lines 1a through 1h', valueRe: /1z\s+(-?[\d,]+)\s*\./, window: 200 }
```

Each entry names:
- **`key`** — where the parsed value lands on the result object (`r.wages`)
- **`scope`** — which section of the document it's allowed to search (see §2.2)
- **`anchor`** — a stable phrase from the form's own boilerplate text, matched whitespace-tolerantly (`flexRe`, since PDF generators often insert extra spacing for column alignment)
- **`valueRe`** — the number pattern to find *after* the anchor, within `window` characters
- **`negate`** (optional) — some Schedule 1 lines print their value in parentheses to indicate subtraction; this flag strips the parens and flips the sign

`applyFieldSchema()` walks the table once and fills `r` for every entry.

Fields that don't fit this single-anchor shape are parsed directly in `parseReturn()`: tax year (needs a fallback chain, since generators sometimes split "2025" into two text tokens), Ordinary Dividends (needs a two-phrase compound anchor), Total Income (multi-tier fallback, since the exact wording shifts slightly by year), the four-voucher Form 2210 payment schedule (a multi-value line, not one number), and the taxpayer's name.

### 2.2 Scoping — why it matters

Four scopes exist: `full` (the whole document), `sch1` (from Schedule 1's own header onward), `schA` (from Schedule A's header onward), `qd` (from the Qualified Dividends & Capital Gain Tax Worksheet's header onward).

Scoping isn't cosmetic — it's a direct response to two real bugs found during testing:
- Schedule 1-A's tips-deduction worksheet (new for 2025) reuses near-identical boilerplate ("Subtract line X from line Y... enter -0-") to the QDCGT worksheet, and appears *earlier* in the document. An unscoped search locked onto the wrong worksheet's numbers.
- Schedule 1's own line-number wording ("1", "3", "5"...) can collide with unrelated parts of the document if searched from the start.

Every field now declares its scope up front rather than defaulting to a document-wide search, so this class of bug can't silently recur when a new field is added.

### 2.3 Schedule 1, line by line

Form 1040 line 8 ("Additional income from Schedule 1") is just Schedule 1's own line-10 grand total — which can be driven by any of Schedule 1's lines 1–7 (state refunds, alimony, business income, other gains/losses, rental/royalty/partnership income, farm income, unemployment) or line 9 (the sum of ~22 named "other income" sub-items, 8a–8z: gambling, cancellation of debt, scholarships, jury duty pay, digital assets, etc.). Reporting the whole total under a generic "Schedule 1" label mislabels it whenever the money actually came from, say, rental income rather than "other income."

The app parses all of these individually by name. Line 9's sub-items (8a–8z) are parsed as a full named list (`SCH1_LINE8_ITEMS`), with three of them (8a, 8d, 8s) requiring parenthesis-and-negate handling since they're subtracted rather than added.

**Safety fallback:** if the parsed 8a–8z sub-items don't sum to the reported line-9 total (an anchor didn't match, or the return has a sub-item this app doesn't recognize by name), the report falls back to showing the generic aggregate rather than silently under-reporting income.

### 2.4 Filing status detection

Two independent paths, in priority order:

1. **Checkbox on Form 1040's Filing Status row.** Works only when the PDF generator renders the checked box's mark as an actual text character (varies by generator — some draw it as vector graphics, invisible to text extraction).
2. **Standard-deduction inference**, when (1) fails. The reported deduction (Form 1040, line 12e) is compared against each status's base amount for that tax year, **plus 0–4 age-65+/blind additions** (a filer can check up to 4 such boxes). The per-box addition amount differs by year and by status group:

   | Year | Single/HOH addition | Married (MFJ/MFS/QSS) addition |
   |------|---------------------|----------------------------------|
   | 2020 | $1,650 | $1,300 |
   | 2021 | $1,700 | $1,350 |
   | 2022 | $1,750 | $1,400 |
   | 2023 | $1,850 | $1,500 |
   | 2024 | $1,950 | $1,550 |
   | 2025 | $2,000 | $1,600 |
   | 2026 | $2,050 | $1,650 |

   Single and MFS **share the same base deduction amount** but use *different* per-box rates — MFS uses the married rate even though its base equals Single's. A filer's `hasSpouse` flag (detected from the presence of a spouse SSN pattern near "Spouse's social security number") disambiguates which rate to test.

   This inference path matters most for Form 1040-SR, which exists specifically for age-65+ filers — meaning an age addition is the *common* case there, not an edge case.

### 2.5 Form 1040 vs. 1040-SR

1040-SR shares identical line numbering with the standard 1040 — only the masthead text differs ("1040-SR ... Tax Return for Seniors" vs. "1040 ... Individual Income Tax Return"). Only the tax-year detection branches on this; every other anchor works unchanged on either form.

---

## 3. Validation layer

`validateReturn(r)` runs automatically after every parse and cross-checks results against the return's own internal arithmetic. It does not prove every field is correct — only that these specific checks didn't catch a problem. Currently checked:

| Check | Formula |
|---|---|
| Income lines reconcile | Sum of all Form 1040 line-9 addends = reported total income |
| Schedule 1 sub-items reconcile | Sum of parsed 8a–8z items = Schedule 1's own line-9 total |
| QDCGT worksheet reconciles | 15%-bracket tax + 20%-bracket tax + ordinary-rate tax = worksheet line 25 |
| AGI reconciles | Total income − adjustments = reported AGI |
| Taxable income reconciles | AGI − deduction − QBI deduction − Schedule 1-A deduction = reported taxable income |

Any mismatch is surfaced as a visible, printed-in-the-PDF warning box at the top of the report (not just a console log), naming which check failed and by how much:

> ⚠ 1 parsing check didn't reconcile against this return's own totals:
> - AGI minus deductions ($492,543) doesn't match the reported taxable income ($485,258).

This layer already caught one real bug during development: a 2024-vs-2025 form-layout difference (QBI deduction is line "13" on 2024's form, "13a" on 2025's restructured form) that a single hardcoded anchor missed.

---

## 4. Report sections

Every section opens with a heading and, for four of them, a one-line description; either way the same fixed gap separates the heading block from the table/chart beneath it, so a section with a description doesn't end up visually tighter than one without.

Rendered in this order, each conditional as noted:

1. **Header** — tax year, taxpayer name, filing status, AGI, taxable income, total tax, effective rate. Print/Save and Start Over buttons.
2. **Parsing warnings box** — only if `validateReturn` found a mismatch.
3. **Income Breakdown** — table + pie chart. Every income type (wages, interest, dividends, IRA, pensions, Social Security, capital gain, all Schedule 1 items) is shown individually if it's ≥ the person's chosen cutoff % of total income; everything under the cutoff is combined into one "Other Income" line. Ordinary Dividends gets one special rule: it's evaluated as a whole first, and only splits into Qualified vs. non-qualified components if the *whole* clears the cutoff — and each resulting piece is then independently re-tested against the cutoff (a piece that ends up small still gets folded into Other Income rather than shown standalone).
4. **Tax Breakdown — QDCGT Worksheet** — only if the return actually used this worksheet (has qualified dividends or capital gains).
5. **Ordinary Income Tax Bracket Breakdown** — only if there's tax liability (`totalTax > 0`); skipped entirely for $0-tax returns.
6. **Deductions** — a single "Standard Deduction" line, or an itemized Schedule A breakdown (categories ≥ cutoff shown individually, rest combined into "Misc."). If the itemized categories don't sum to the reported deduction, falls back to showing just the verified total rather than a possibly-wrong breakdown.
7. **Net Investment Income Tax (NIIT)** — only if present.
8. **Foreign Tax Credit** — only if present.
9. **Tax Payment Schedule — Form 2210 / Schedule AI** — only if the return includes the AI (annualized-income) schedule.
10. **Remaining Tax Owed / Refund**.

**Implementation note:** every section's markup is assembled from five small shared helpers rather than each section hand-building its own HTML: `swatchRow`/`totalRow` (a labeled row with a color swatch, and the bold total row beneath it), `dataTable` (wraps rows in a `<table>` under a given header), `tableWithChart` (the two-column table + pie-chart layout used by Income Breakdown, Tax Exempt Income, Tax Breakdown, Bracket Breakdown, and itemized Deductions), and `kvTable` (the compact single-row "Item / Amount" table used by Standard Deduction, NIIT, Foreign Tax Credit, and Refund/Owed). This keeps every section's table structurally identical and means a new breakdown section doesn't require re-deriving the same string-concatenation pattern by hand.

---

## 5. Pie chart rendering

Custom inline-SVG renderer (`renderPie`), no charting library. Labels are placed inside a slice when it's wide enough (≥32°), otherwise outside with a leader line. When several small slices land close together in angle (common once Schedule 1 sub-items are broken out), their outside labels stagger outward in radius so they don't visually collide. A pie slice can't represent a negative value (a capital loss, a negative Schedule 1 adjustment), so negative-valued rows appear in the table but are excluded from the chart, with a footnote explaining why.

---

## 6. Print / Save PDF

The button calls `window.print()` **directly on the current page** whenever this file is *not* running inside an iframe (`window.self === window.top`) — covering local files, direct hosting, and most "embed a page" setups. No extra tab, no popup.

Only when the page detects it's genuinely embedded in a sandboxed `<iframe>` (e.g. a Google Sites "Embed code" gadget, which often omits `allow-modals` from the sandbox and silently blocks `window.print()` from inside the frame) does it fall back to: serialize the finished report into its own standalone document and open it as a real link-click navigation in a fresh top-level tab — a kind of user-initiated navigation that sandboxed iframes typically still allow even when they block direct printing. That tab auto-triggers its own print dialog on load, and its Print button (the only one shown there) has its own inline handler so it stays functional if the person dismisses the dialog and wants to retry.

---

## 7. Known limitations

- **Vector-graphics checkboxes.** Some PDF generators draw a checked filing-status box as vector lines rather than a text glyph, invisible to text extraction. The standard-deduction inference path (§2.4) covers this, but is itself unreliable if the deduction amount is ambiguous between statuses.
- **Schedule A breakdown is unverified against a real itemized return.** The category-level anchors (medical, taxes paid, interest, charity, casualty, other) were written from the form's known structure but never tested against an actual PDF that itemizes — the reconciliation-fallback (§4, section 6) exists specifically to avoid showing a wrong breakdown in that case.
- **Not every Schedule 1 line-8 sub-item is guaranteed correct across all PDF generators** — only the ones exercised by real test returns (scholarships, on Jeffrey Cheng's return) have been confirmed against ground truth. The reconciliation fallback (§2.3) is the safety net for the other ~21.
- **No OCR** — relies entirely on the PDF's text layer. A scanned/flattened image-only PDF produces no data.
- **Standard-deduction inference doesn't yet cover 2026.** `STD_DEDUCTION` (used only for the fallback filing-status inference in §2.4, when the checkbox glyph isn't extractable) is populated through 2025; a 2026 return without a readable checkbox falls back to "unknown" filing status and requires manual selection, even though 2026 is otherwise a fully supported tax year (bracket tables and age/blind additions are both current through 2026).

## 8. Test coverage

Verified end-to-end (parsing → income-breakdown reconciliation → filing status → deductions → zero false-positive validation warnings) against five real, distinct returns:

| Return | Year | Form | Status | Notable coverage |
|---|---|---|---|---|
| 24.pdf | 2024 | 1040 | MFJ | Baseline; QBI deduction (line 13, no "a") |
| 25.pdf | 2025 | 1040 | MFJ | Baseline, next year's brackets |
| Fed_File.pdf | 2025 | 1040-SR | Single | Age-65+ deduction addition; rental income (Sch. 1, line 5) |
| File.pdf | 2025 | 1040 | Single | Student; taxable scholarship (Sch. 1, line 8r); Schedule 1-A present |
| Fed.pdf | 2025 | 1040-SR | MFS | Two spouses' SSNs both present; $0 tax liability; MFS-specific deduction rate |
