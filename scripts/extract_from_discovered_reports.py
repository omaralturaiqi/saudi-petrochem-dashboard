#!/usr/bin/env python3
"""
scripts/extract_from_discovered_reports.py

Dry-run, in-memory, multi-report financial extraction for 4 real annual
report PDFs discovered by scripts/discover_company_annual_reports.py's
prior live Render Shell run (HEAD-verified: content_type=application/pdf).
No PDF is ever written to disk. No Neon connection of any kind. Prints
candidate financial facts for human review only.

REUSE, NOT REINVENTION — read this before assuming what this script
imports: this script does NOT import CONCEPTS / line_matches() /
looks_like_financial_value() from parser.py directly, and it does NOT
call parser.py's extract_company(). Both are structurally unusable here:
extract_company(pdf_path, ...) requires a real on-disk Path (it calls
sha256_file(path), which opens the path directly, and
path.relative_to(RAW_DIR), which requires the path to live under a
hardcoded RAW_DIR) — incompatible with this task's explicit in-memory-
only, zero-disk-write requirement, and modifying parser.py itself is
explicitly out of scope for this task.

Instead, this script imports and reuses
ingestion.extract_dry_run.extract_from_bytes() / verify_pdf_bytes() /
PdfBytesInvalid — the module already built and proven for exactly this
purpose (see scripts/dry_run_extract.py, its existing single-report CLI).
That module's own docstring states it "reuses parser.py's existing
CONCEPTS / line_matches() / looks_like_financial_value() / parse_number()
rather than reimplementing the keyword-matching/number-parsing logic" —
imported there directly (`from parser import CONCEPTS, line_matches,
looks_like_financial_value, parse_number`, ingestion/extract_dry_run.py
line 99) and verbatim, not reimplemented, not modified. This script is
one level further removed from parser.py than that literal import, but
zero lines of parser.py's own matching/parsing logic are duplicated
anywhere in this file or in ingestion/extract_dry_run.py — see
IMPORT_FROM_PARSER in this task's final report for the exact chain.

extract_from_bytes() is also strictly BETTER suited to this task than
extract_company() would have been even if the Path issue didn't exist:
it is coordinate-aware (binds each numeric value to a specific fiscal-
year column via word x-positions, rather than assuming the first/second
number found is the right one) and never populates value_col1/value_col2
unless that binding actually succeeded — the same "no assumed year
column, explicit warning on ambiguity, never fabricate a value"
discipline this task explicitly asked for, already built and already
exercised against a real filing (see ingestion/extract_dry_run.py's own
docstring for the full mechanism).

WHAT THIS SCRIPT DOES:
  1. For each entry in REPORTS_TO_EXTRACT: ONE GET request. The response
     body (resp.content) is kept ONLY as an in-memory bytes object —
     never written to a file, never passed as a filesystem path anywhere.
  2. verify_pdf_bytes() checks the bytes actually start with the PDF
     magic number (%PDF-) before any parsing is attempted — catches an
     HTML error/block page fetched instead of a real PDF cleanly.
  3. extract_from_bytes(pdf_bytes, ticker, fiscal_year) — pdfplumber
     opens io.BytesIO(pdf_bytes) directly (see that function's own
     docstring: "uses io.BytesIO, not a temp file"). Local variables
     holding pdf_bytes are set to None immediately after this call
     returns, in every code path (success or exception) — see
     extract_one_report()'s try/finally.
  4. Prints, per report: document SHA-256, page count, total candidates,
     known-vs-unknown concept counts, year-resolved-vs-not counts, and
     every candidate's concept/label/value_col1/value_col2/confidence/
     raw_text/warnings — identical reporting shape to
     scripts/dry_run_extract.py's existing single-report output, so the
     same "how do I read this" mental model applies to all 4 reports.
  5. NO NEON CONNECTION ANYWHERE IN THIS FILE — same structural
     guarantee ingestion/extract_dry_run.py's own docstring states for
     itself: nothing in this script imports psycopg, NEON_CONNECTION_
     STRING, app.py, or us_xbrl_api.py, so it cannot reach a database
     even if it tried to.

WHAT THIS SCRIPT DELIBERATELY DOES NOT DO:
  - Never writes a PDF (or any extracted intermediate) to disk at any
    point — no data/raw/ write, no temp file. There is therefore nothing
    to "clean up" after each report; the guarantee is "never created it
    in the first place", not "created then deleted".
  - Never modifies parser.py or ingestion/extract_dry_run.py.
  - Never stops the whole run because one report's fetch/verify/extract
    failed — each report is wrapped in its own try/except in main();  a
    failure is printed clearly as FAILED with the real exception, and
    the loop continues to the next report. See main()'s summary, which
    reports success/failure counts for all 4, not just the ones that
    happened to work.
  - Never fabricates a fiscal-year value: exactly like
    scripts/dry_run_extract.py, value_col1/value_col2 stay whatever
    extract_from_bytes() itself determined (None when year_resolved is
    False) — this script does not second-guess or backfill that.

USAGE:
    python3 scripts/extract_from_discovered_reports.py
        # no environment variables required — no Neon connection of any
        # kind. Needs real network access to each company's own PDF URL;
        # this authoring sandbox does not have that (WebFetch confirmed
        # fully blocked here this session); intended to run via Render
        # Shell, per this task's own instructions.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

# Same sys.path setup scripts/dry_run_extract.py already uses, so
# `ingestion.extract_dry_run` (and, transitively, `parser`) import
# correctly whether this script is run directly or as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.extract_dry_run import PdfBytesInvalid, extract_from_bytes, verify_pdf_bytes

# 4 real, HEAD-verified (content_type=application/pdf) URLs discovered by
# scripts/discover_company_annual_reports.py's prior live Render Shell
# run — supplied directly for this task, not fetched/verified again by
# Claude in this authoring sandbox (no network access here — see USAGE).
REPORTS_TO_EXTRACT: list[dict] = [
    {
        "ticker": "2280", "company": "Almarai", "fiscal_year": 2024,
        "url": "https://almmedia.almarai.com/FinancialReports/Annual_Report_2024_EN418202680525AM.pdf",
    },
    {
        "ticker": "6004", "company": "CATRION", "fiscal_year": 2024,
        "url": "https://www.catrion.com/application/files/2417/4298/1965/CATRION_Annual_Report_2024_ENG.pdf",
    },
    {
        "ticker": "1120", "company": "Al Rajhi Bank", "fiscal_year": 2023,
        "url": "https://www.alrajhibank.com.sa/-/media/Project/AlRajhi/ARBRevamp/Investor-Relation/Annual-Reports/Annual-Report-EN-2023.pdf",
    },
    {
        "ticker": "7010", "company": "STC", "fiscal_year": 2025,
        "url": "https://www.stc.com/content/dam/groupsites/en/pdf/stc2025-annual-report-en.pdf",
    },
]

FETCH_TIMEOUT_SECONDS = 60  # same extended value already established for
                             # heavy Saudi corporate-site PDFs this session
                             # (see scripts/discover_company_annual_reports.py's
                             # EXTENDED_REQUEST_TIMEOUT_SECONDS) — applied to
                             # all 4 here since every one of these is itself
                             # a full annual-report PDF, not a lightweight page.


def extract_one_report(report: dict) -> dict:
    """Fetch + verify + extract exactly ONE report, fully in-memory.
    Raises on any failure (network, invalid PDF bytes, or extraction
    error) — the caller (main()) is responsible for catching that per
    report so one failure does not abort the whole run, per this task's
    explicit requirement."""
    import requests

    ticker = report["ticker"]
    company = report["company"]
    fiscal_year = report["fiscal_year"]
    url = report["url"]

    print("=" * 78)
    print(f"EXTRACT: {company} (ticker={ticker}) FY{fiscal_year}")
    print(f"URL: {url}")
    print("=" * 78)

    pdf_bytes: bytes | None = None
    try:
        resp = requests.get(url, timeout=FETCH_TIMEOUT_SECONDS)
        resp.raise_for_status()
        pdf_bytes = resp.content
        print(f"HTTP status   : {resp.status_code}")
        print(f"Content-Type  : {resp.headers.get('Content-Type', 'unknown')}")
        print(f"Response size : {len(pdf_bytes)} bytes")
        resp = None  # response object discarded; only the raw bytes are kept from here on

        verify_pdf_bytes(pdf_bytes)
        print("PDF magic bytes: valid (%PDF-)")

        result = extract_from_bytes(pdf_bytes, ticker=ticker, fiscal_year=fiscal_year)

        candidates = result["candidates"]
        print(f"Document SHA-256 : {result['document_sha256']}")
        print(f"Page count        : {result['page_count']}")
        print(f"Total candidates  : {len(candidates)}")

        known = [c for c in candidates if c["concept_known"]]
        unknown = [c for c in candidates if not c["concept_known"]]
        resolved = [c for c in candidates if c["year_resolved"]]
        unresolved = [c for c in candidates if not c["year_resolved"]]
        print(f"Known concepts    : {len(known)}")
        print(f"Unknown concepts  : {len(unknown)}")
        print(f"Year-resolved (value_col1/2 trustworthy)   : {len(resolved)}")
        print(f"Year NOT resolved (value_col1/2 withheld)  : {len(unresolved)}")
        print()

        print("-" * 78)
        print("CANDIDATES")
        print("-" * 78)
        for c in candidates:
            print(f"[page {c['source_page']}] {c['concept']} "
                  f"(known={c['concept_known']}, confidence={c['confidence']}, "
                  f"year_resolved={c['year_resolved']})")
            print(f"  label     : {c['reported_label']!r}")
            print(f"  value_col1: {c['value_col1']}")
            print(f"  value_col2: {c['value_col2']}")
            print(f"  unit      : {c['unit']} {c['currency']}")
            print(f"  raw_text  : {c['raw_text']!r}")
            for w in c["warnings"]:
                print(f"  WARNING   : {w}")
            print()

        print("=" * 78)
        print(f"{company}: extraction complete. No file written to disk at any point.")
        print("=" * 78)
        return {
            "ticker": ticker, "company": company, "fiscal_year": fiscal_year,
            "status": "SUCCESS", "candidates": candidates,
        }
    finally:
        # Whether this succeeded or raised, drop the local reference to
        # the PDF bytes — nothing else in this process retains them, and
        # nothing was ever written to disk in the first place, so there
        # is no temp file to clean up.
        pdf_bytes = None


def main() -> None:
    print("#" * 78)
    print("EXTRACT FROM DISCOVERED REPORTS — dry-run, in-memory only, no DB writes")
    print("Reuses ingestion.extract_dry_run.extract_from_bytes() (which itself reuses")
    print("parser.py's CONCEPTS/line_matches/looks_like_financial_value/parse_number verbatim)")
    print(f"{len(REPORTS_TO_EXTRACT)} report(s) queued.")
    print("#" * 78)
    print()

    succeeded: list[dict] = []
    failed: list[dict] = []

    for report in REPORTS_TO_EXTRACT:
        try:
            r = extract_one_report(report)
            succeeded.append(r)
        except Exception as e:
            print(f"FAILED: {report['company']} (ticker={report['ticker']}) FY{report['fiscal_year']}: "
                  f"{type(e).__name__}: {e}")
            print("-" * 78)
            traceback.print_exc(file=sys.stdout)
            print("-" * 78)
            print(f"{report['company']}: no file was ever written to disk for this report.")
            print("=" * 78)
            failed.append({
                "ticker": report["ticker"], "company": report["company"],
                "fiscal_year": report["fiscal_year"], "error": f"{type(e).__name__}: {e}",
            })
        print()

    print("#" * 78)
    print("SUMMARY")
    for r in succeeded:
        print(f"  {r['company']} ({r['ticker']}) FY{r['fiscal_year']}: SUCCESS — {len(r['candidates'])} candidates")
    for f in failed:
        print(f"  {f['company']} ({f['ticker']}) FY{f['fiscal_year']}: FAILED — {f['error']}")
    print(f"Succeeded: {len(succeeded)}/{len(REPORTS_TO_EXTRACT)}   Failed: {len(failed)}/{len(REPORTS_TO_EXTRACT)}")
    print("PDFs persisted to disk by this script: 0")
    print("Neon writes issued by this script: 0")
    print("#" * 78)


if __name__ == "__main__":
    main()
