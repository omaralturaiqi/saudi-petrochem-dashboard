"""
Phase 0.5 - Parser v2 (imported into version control during Phase 0 foundation
hardening — this file previously existed only in a working session, per the
audit finding that the ingestion pipeline was not reproducible from the repo).

HONEST STATUS — read before trusting any output of this script:

WHAT WORKS WELL (verified against official filings):
  - YANSAB FY2024/2023: 9/9 values extracted matched the official annual
    report exactly (revenue, net_income, eps_basic, total_assets, total_equity).
  - Advanced Petrochemical FY2024/2023: after two rounds of fixing (see below),
    net_income and eps_basic extracted correctly, matching the official filing
    exactly (-259,180 / 171,061 thousand SAR; -1.00 / 0.66 EPS).

KNOWN, CONFIRMED FAILURE MODES (do not silently trust output without these
being addressed or the row being manually reviewed):
  1. Label-phrasing variance across companies. YANSAB uses "NET INCOME FOR
     THE YEAR"; Advanced uses "(Loss) profit for the year attributable to
     equity holders of the Parent Company". A single fixed regex missed
     Advanced entirely in the first version of this parser. The current
     keyword-group matching is more tolerant but is NOT proven to generalize
     to companies not yet tested (SABIC, Saudi Kayan, and the other 6 target
     companies have not had their concept coverage fully validated against
     official filings for every concept — only spot-checked, see project
     history).
  2. FALSE POSITIVE risk, not just false negative: an earlier version of this
     parser matched a real number for Advanced's net_income concept, but it
     was a *different company's* figure (an associate/joint-venture's loss),
     not the consolidated Group figure — a wrong answer with high apparent
     confidence, worse than a clean miss. This was fixed with an explicit
     exclusion keyword list (associate, joint venture, share of loss, etc.)
     but that list is NOT guaranteed exhaustive for companies not yet tested.
  3. Note-reference contamination: SABIC's tables include inline note numbers
     (e.g. "27") adjacent to real financial figures; naive number-extraction
     captured "27" as if it were a value. Mitigated by requiring a comma or
     decimal point to treat a token as a real financial value — but a
     single-digit or unformatted note reference that happens to exceed the
     4-digit fallback threshold could theoretically still slip through and
     has not been exhaustively tested against every company's note style.
  4. The `revenue` concept currently requires the word "total" to appear on
     the matched line, to disambiguate primary statement lines from segment
     sub-totals. This happened to still work for YANSAB and SABIC (both have
     a "Total revenue" line somewhere in their notes) but is a fragile,
     company-specific coincidence, not a generalizable rule — a company
     without any "Total revenue" phrasing anywhere in its filing would
     silently produce a MISS for this concept with this version of the code.
  5. This remains text-line extraction (pdfplumber .extract_text() + regex/
     keyword matching on lines), not structural table extraction (camelot/
     tabula). Multi-line label wrapping is partially handled via a 2-line
     sliding window, but this is a heuristic, not a robust table parser.

WHAT THIS MEANS FOR USING THIS SCRIPT:
  - Every row this script produces already carries a `confidence` field
    (HIGH/MEDIUM/LOW) and `raw_text` for audit — per this project's existing
    rule, LOW confidence rows should not enter analysis without human review
    (see core.financial_line_items.reviewed_by_human).
  - Do NOT treat a clean run (no errors) as equivalent to "verified accurate."
    Verification means cross-checking against the official PDF by page number,
    as was done for YANSAB and Advanced — this has NOT yet been done for the
    other 8 target companies.
  - Structural table extraction (camelot/tabula) was investigated but not
    adopted in this version — ghostscript, a camelot dependency, was not
    available in the environment this was developed in. This remains a
    documented open item for a future phase, not something silently dropped.

Dependencies: pdfplumber (see ingestion/requirements-ingestion.txt — kept
separate from the web app's requirements.txt so the Render web service build
is unaffected by ingestion-only dependencies).
"""

#!/usr/bin/env python3
"""
Phase 0.5 - Parser v2
Fixes documented in v1's honest failure log:
  1. ADVANCED miss: exact regex anchors didn't match "(Loss) profit for the
     year attributable..." or "(Losses) earnings Per Share". Fix: keyword-set
     matching (all keywords present, any order/case) instead of anchored regex.
  2. SABIC contamination: note-reference numbers (e.g. "27") got captured as
     financial values. Fix: a candidate number must look like a real financial
     figure - either has a thousands separator (comma) or a decimal point, or
     is a bare negative in parentheses. Bare 1-3 digit integers with no comma
     are treated as note references and discarded, not just deprioritized.
This is still text-line extraction (not full structural table parsing) -
that remains a known limitation, disclosed as such in the report.
"""
import pdfplumber
import re
import hashlib
import csv
from pathlib import Path
from datetime import datetime, timezone

RAW_DIR = Path("/home/claude/data/raw")
OUT_DIR = Path("/home/claude/data/processed")
OUT_DIR.mkdir(exist_ok=True, parents=True)

# concept -> (keyword groups [ALL groups must match, ANY keyword in a group matches],
#             statement_type, require_any_of_these, exclude_if_any_of_these)
CONCEPTS = [
    ("revenue", [["revenue"]], "income_statement", {"total"}, []),
    ("gross_profit", [["gross profit", "gross (loss)"]], "income_statement", set(), []),
    ("operating_income", [["operating profit", "operating (loss)", "income from operations", "income / (loss) from operations"]], "income_statement", set(), []),
    ("net_income", [["net income", "profit for the year", "loss) profit for the year", "loss for the year"]], "income_statement", set(),
        ["associate", "joint venture", "share of loss", "share of profit", "share in results", "group's share", "sk advanced", "subsidiar"]),
    ("eps_basic", [["per share"], ["sr"]], "income_statement", set(), []),
    ("total_assets", [["total assets"]], "balance_sheet", set(), []),
    ("total_equity", [["total equity"]], "balance_sheet", set(), []),
    ("total_liabilities", [["total liabilities"]], "balance_sheet", set(), []),
    ("cash_and_equivalents", [["cash and cash equivalents"]], "balance_sheet", set(), []),
    ("cfo", [["net cash", "cash flows from operating", "cash generated from operating"], ["operating"]], "cash_flow", set(), []),
    ("capex", [["purchase of property"]], "cash_flow", set(), []),
]

# A token counts as a real value if: has comma (thousands sep), OR has a decimal point,
# OR is wrapped in parens as a negative AND has >=4 digits (e.g. "(259,180)" already has comma so covered,
# but also cover 4+ digit negatives without commas just in case)
def looks_like_financial_value(tok):
    raw = tok.strip()
    neg = raw.startswith("(") and raw.endswith(")")
    core = raw.strip("()")
    if "," in core:
        return True
    if "." in core and len(core.replace(".", "").replace("-", "")) >= 2:
        # decimal number like 0.75, -1.00, 32.52 - treat as value (EPS-scale)
        return True
    if neg and core.replace(",", "").isdigit() and len(core.replace(",", "")) >= 4:
        return True
    return False

NUM_TOKEN_RE = re.compile(r"\(?-?[\d,]+\.?\d*\)?")

def parse_number(tok):
    tok = tok.strip().replace(",", "")
    neg = tok.startswith("(") and tok.endswith(")")
    tok = tok.strip("()")
    try:
        val = float(tok)
        return -val if neg else val
    except ValueError:
        return None

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def line_matches(line_lower, keyword_groups):
    for group in keyword_groups:
        if not any(kw in line_lower for kw in group):
            return False
    return True

def extract_company(pdf_path, company_id, ticker, fiscal_year):
    results = []
    file_hash = sha256_file(pdf_path)
    with pdfplumber.open(pdf_path) as pdf:
        page_count = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            if not text:
                continue
            lines = text.split("\n")
            # build 2-line sliding windows so a label wrapping onto the next
            # physical line (common in these PDFs) still gets matched to its
            # numbers, e.g. "...attributable to equity holders of the Parent"
            # / "Company (259,180) 171,061"
            windows = [lines[i] for i in range(len(lines))]
            windows2 = [lines[i] + " " + lines[i + 1] if i + 1 < len(lines) else lines[i]
                        for i in range(len(lines))]

            for concept, keyword_groups, stmt_type, require_any, exclude_any in CONCEPTS:
                for line, window in zip(lines, windows2):
                    line_lower = line.lower()
                    window_lower = window.lower()
                    if not line_matches(line_lower, keyword_groups):
                        continue
                    if require_any and not any(r in line_lower for r in require_any):
                        continue
                    if exclude_any and any(x in window_lower for x in exclude_any):
                        continue
                    raw_tokens = NUM_TOKEN_RE.findall(window)
                    good_tokens = [t for t in raw_tokens if looks_like_financial_value(t)]
                    if not good_tokens:
                        continue
                    parsed_vals = [parse_number(t) for t in good_tokens]
                    parsed_vals = [v for v in parsed_vals if v is not None]
                    if not parsed_vals:
                        continue
                    confidence = "MEDIUM" if len(parsed_vals) <= 3 else "LOW"
                    results.append({
                        "company_id": company_id,
                        "ticker": ticker,
                        "statement_type": stmt_type,
                        "concept": concept,
                        "reported_label": window.strip()[:90],
                        "value_col1": parsed_vals[0] if len(parsed_vals) > 0 else None,
                        "value_col2": parsed_vals[1] if len(parsed_vals) > 1 else None,
                        "currency": "SAR",
                        "unit": "thousand",
                        "fiscal_year": fiscal_year,
                        "source_document": str(pdf_path.relative_to(RAW_DIR)),
                        "source_page": i + 1,
                        "extraction_method": "pdfplumber_text_keyword_v2",
                        "confidence": confidence,
                        "is_restated": "UNKNOWN",
                        "raw_text": window.strip()[:150],
                        "document_sha256": file_hash,
                    })
    return results, page_count, file_hash

if __name__ == "__main__":
    jobs = [
        ("YANSAB_2290/2024/annual_report.pdf", "YANSAB", "2290", 2024),
        ("ADVANCED_2330/2024/annual_report.pdf", "ADVANCED", "2330", 2024),
        ("SABIC_2010/2024/annual_report.pdf", "SABIC", "2010", 2024),
    ]
    all_results = []
    for rel_path, company_id, ticker, fy in jobs:
        pdf_path = RAW_DIR / rel_path
        if not pdf_path.exists():
            print(f"MISSING: {pdf_path}")
            continue
        print(f"Parsing {company_id} FY{fy} ...")
        results, page_count, file_hash = extract_company(pdf_path, company_id, ticker, fy)
        all_results.extend(results)
        print(f"  -> {len(results)} candidate lines extracted")

    if all_results:
        keys = list(all_results[0].keys())
        with open(OUT_DIR / "normalized_financials_v2.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(all_results)
    print(f"\nTotal v2 extracted rows: {len(all_results)}")
