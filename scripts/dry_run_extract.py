#!/usr/bin/env python3
"""
scripts/dry_run_extract.py

READ-ONLY, in-memory dry-run: fetches a report PDF's bytes over HTTPS,
verifies them, extracts candidate financial facts via
ingestion.extract_dry_run.extract_from_bytes(), and prints a structured
report.

DOES NOT:
  - save the PDF to disk, ever (bytes live only in local variables for the
    duration of this process; no data/raw/ write, no temp file)
  - import any database/Neon code (no psycopg, no NEON_CONNECTION_STRING,
    no app.py, no us_xbrl_api.py)
  - write to source_documents / financial_line_items / any table
  - modify SABIC_SOURCE_REGISTRY or any other registry/config file
  - import ingestion.load_historical at all — deliberately decoupled from
    that module's surface; the target URL is a CLI argument/default here,
    not read from the registry, per explicit instruction.

USAGE:
    python3 scripts/dry_run_extract.py
        # uses the default (verified SABIC FY2024) URL/ticker/fiscal-year,
        # processing every page (identical to omitting --start-page/--end-page)
    python3 scripts/dry_run_extract.py --url URL --ticker TICKER --fiscal-year YYYY
    python3 scripts/dry_run_extract.py --start-page 50 --end-page 60
        # DIAGNOSTIC ONLY: process just pages 50-60 (1-based, inclusive) of
        # the same document, to isolate which page a failure occurs on.
        # The PDF is still downloaded and opened normally either way.
    python3 scripts/dry_run_extract.py --period-type Q2 --fiscal-quarter 2
        # Period-aware column selection IS implemented (see
        # ingestion/extract_dry_run.py's own docstring — "PERIOD-BLOCK
        # DISAMBIGUATION"). When a table block's year-header row has more
        # than one column sharing the same bare year (e.g. a discrete
        # three-month figure and a cumulative six-month/YTD figure both
        # labeled "2026"), the parser uses a detected period-label row
        # above the header (e.g. "For the three-month period ..." / "For
        # the six-month period ...") plus x-position clustering to
        # distinguish which column the requested period_type refers to:
        # Q1/Q2/Q3/Q4 select the "three-month" block, H1 selects the
        # "six-month" block. This has been verified against a real
        # quarterly filing for Q2 and H1 specifically (see
        # ingestion/extract_dry_run.py's docstring and
        # tests/test_extract_dry_run.py's TestPeriodBlockDisambiguation*
        # classes); the Q1/Q3/Q4 mapping exists in code but has not been
        # verified against real Q1/Q3/Q4 filing bytes. A table with only
        # one period block per year (e.g. a normal annual report, or any
        # document where no period-label row is found) is unaffected —
        # FY/annual single-block extraction behaves exactly as before.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.extract_dry_run import (
    VALID_PERIOD_TYPES,
    PdfBytesInvalid,
    extract_from_bytes,
    verify_pdf_bytes,
)

# The verified SABIC FY2024 URL currently in
# SABIC_SOURCE_REGISTRY[("2010", 2024)] (ingestion/load_historical.py).
# Duplicated here as a literal DEFAULT — NOT imported from the registry —
# to keep this script decoupled from load_historical.py's module surface,
# per explicit instruction. If the registry URL is ever changed, this
# default should be updated to match; it is not read dynamically.
DEFAULT_SABIC_FY2024_URL = (
    "https://www.sabic.com/en/Images/SABIC-Integrated-Annual-Report-2024-EN_tcm1010-46870.pdf"
)
DEFAULT_TICKER = "2010"
DEFAULT_FISCAL_YEAR = 2024


def run(
    url: str,
    ticker: str,
    fiscal_year: int,
    start_page: int | None = None,
    end_page: int | None = None,
    period_type: str = "FY",
    fiscal_quarter: int | None = None,
) -> dict | None:
    import requests  # local import — keeps module import itself side-effect-free

    print("=" * 78)
    print("DRY-RUN EXTRACTION — read-only, in-memory only")
    print("=" * 78)
    print(f"URL          : {url}")
    print(f"Ticker       : {ticker}")
    print(f"Fiscal year  : {fiscal_year}")
    print(f"Period type  : {period_type}"
          + (f" (fiscal_quarter={fiscal_quarter})" if fiscal_quarter is not None else ""))
    if period_type != "FY":
        print("  NOTE: period-aware column selection is implemented (see "
              "ingestion/extract_dry_run.py's own docstring, 'PERIOD-BLOCK "
              "DISAMBIGUATION') — for a table block whose year-header row has "
              "more than one column sharing this fiscal year, this run will use "
              "detected period-label text to select the correct block: Q1-Q4 "
              "select the 'three-month' block, H1 selects the 'six-month' "
              "block. Verified against real filing bytes for Q2/H1 specifically; "
              "Q1/Q3/Q4 mappings exist in code but are not yet verified against "
              "real Q1/Q3/Q4 filing bytes. A table with only one period block "
              "for this year (e.g. a normal annual table) is unaffected by "
              "period_type and resolves the same as an FY request.")
    if start_page is not None or end_page is not None:
        print(f"Page range   : {start_page if start_page is not None else 1}"
              f"-{end_page if end_page is not None else '(last)'} (diagnostic subset)")
    print()

    # Cheap, pre-fetch sanity checks on the requested range — these don't
    # need the document's real page count, so fail immediately rather than
    # downloading a large PDF first just to reject an obviously-malformed
    # range. Full validation against the real page count still happens
    # inside extract_from_bytes(), which is the only place that knows it.
    if start_page is not None and start_page < 1:
        print(f"INVALID PAGE RANGE: --start-page must be >= 1, got {start_page}")
        print("=" * 78)
        print("DRY-RUN ONLY — no PDF persisted, no database access, no database writes.")
        print("=" * 78)
        sys.exit(2)
    if end_page is not None and end_page < 1:
        print(f"INVALID PAGE RANGE: --end-page must be >= 1, got {end_page}")
        print("=" * 78)
        print("DRY-RUN ONLY — no PDF persisted, no database access, no database writes.")
        print("=" * 78)
        sys.exit(2)
    if start_page is not None and end_page is not None and start_page > end_page:
        print(f"INVALID PAGE RANGE: --start-page ({start_page}) must be <= --end-page ({end_page})")
        print("=" * 78)
        print("DRY-RUN ONLY — no PDF persisted, no database access, no database writes.")
        print("=" * 78)
        sys.exit(2)

    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
    except Exception as e:
        print(f"FETCH FAILED: {type(e).__name__}: {e}")
        print("=" * 78)
        print("DRY-RUN ONLY — no PDF persisted, no database access, no database writes.")
        print("=" * 78)
        sys.exit(1)

    pdf_bytes = resp.content
    print(f"HTTP status    : {resp.status_code}")
    print(f"Content-Type   : {resp.headers.get('Content-Type', 'unknown')}")
    print(f"Response size  : {len(pdf_bytes)} bytes")
    print(f"Final URL      : {resp.url}")
    print(f"Redirect count : {len(resp.history)}")
    resp = None  # done with the response object; only the raw bytes are kept from here on

    try:
        verify_pdf_bytes(pdf_bytes)
    except PdfBytesInvalid as e:
        print(f"PDF VALIDATION FAILED: {e}")
        print("=" * 78)
        print("DRY-RUN ONLY — no PDF persisted, no database access, no database writes.")
        print("=" * 78)
        sys.exit(1)
    print("PDF magic bytes: valid (%PDF-)")
    print()

    try:
        result = extract_from_bytes(
            pdf_bytes, ticker=ticker, fiscal_year=fiscal_year,
            start_page=start_page, end_page=end_page,
            period_type=period_type, fiscal_quarter=fiscal_quarter,
        )
    except Exception as e:
        print(f"EXTRACTION FAILED: {type(e).__name__}: {e}")
        print("-" * 78)
        print("TRACEBACK:")
        traceback.print_exc(file=sys.stdout)
        print("-" * 78)
        print("=" * 78)
        print("DRY-RUN ONLY — no PDF persisted, no database access, no database writes.")
        print("=" * 78)
        sys.exit(1)
    pdf_bytes = None  # discard — nothing else in this process retains the PDF bytes

    candidates = result["candidates"]
    print(f"Document SHA-256 : {result['document_sha256']}")
    print(f"Page count       : {result['page_count']}")
    print(f"Total candidates : {len(candidates)}")

    known = [c for c in candidates if c["concept_known"]]
    unknown = [c for c in candidates if not c["concept_known"]]
    print(f"Known concepts   : {len(known)}")
    print(f"Unknown concepts : {len(unknown)}")

    ambiguous = [c for c in candidates if any("not disambiguated" in w for w in c["warnings"])]
    duplicated = [c for c in candidates
                  if any("detected" in w and "times" in w for w in c["warnings"])]
    print(f"Ambiguity warnings        : {len(ambiguous)}")
    print(f"Duplicate-concept warnings: {len(duplicated)}")
    print()

    print("-" * 78)
    print("CANDIDATES")
    print("-" * 78)
    for c in candidates:
        print(f"[page {c['source_page']}] {c['concept']} "
              f"(known={c['concept_known']}, confidence={c['confidence']})")
        print(f"  label     : {c['reported_label']!r}")
        print(f"  value_col1: {c['value_col1']}")
        print(f"  value_col2: {c['value_col2']}")
        print(f"  unit      : {c['unit']} {c['currency']}")
        print(f"  raw_text  : {c['raw_text']!r}")
        for w in c["warnings"]:
            print(f"  WARNING   : {w}")
        print()

    print("=" * 78)
    print("SOURCE PAGES REFERENCED:", sorted({c["source_page"] for c in candidates}))
    print("=" * 78)
    print("DRY-RUN ONLY — no PDF persisted, no database access, no database writes.")
    print("=" * 78)
    return result


def build_arg_parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--url", default=DEFAULT_SABIC_FY2024_URL)
    cli.add_argument("--ticker", default=DEFAULT_TICKER)
    cli.add_argument("--fiscal-year", type=int, default=DEFAULT_FISCAL_YEAR)
    cli.add_argument(
        "--start-page", type=int, default=None,
        help="DIAGNOSTIC ONLY: 1-based inclusive page to start processing from "
             "(default: page 1 — i.e. the whole document, identical to omitting this flag)",
    )
    cli.add_argument(
        "--end-page", type=int, default=None,
        help="DIAGNOSTIC ONLY: 1-based inclusive page to stop processing at "
             "(default: the document's last page — i.e. the whole document, "
             "identical to omitting this flag)",
    )
    cli.add_argument(
        "--period-type", choices=VALID_PERIOD_TYPES, default="FY",
        help="Requested reporting period (mirrors schema.sql's period_type CHECK "
             "domain). REQUEST-LABELING ONLY — quarter-specific column detection "
             "is not implemented; a non-FY value still binds against bare-year "
             "header columns exactly as FY would, just recorded under this label.",
    )
    cli.add_argument(
        "--fiscal-quarter", type=int, choices=[1, 2, 3, 4], default=None,
        help="Requested fiscal quarter (1-4), recorded as a label alongside "
             "--period-type — not cross-validated against it, not used to alter "
             "extraction behavior in this revision.",
    )
    return cli


def main():
    cli = build_arg_parser()
    args = cli.parse_args()
    run(
        args.url, args.ticker, args.fiscal_year, args.start_page, args.end_page,
        args.period_type, args.fiscal_quarter,
    )


if __name__ == "__main__":
    main()
