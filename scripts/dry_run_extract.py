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
        # uses the default (verified SABIC FY2024) URL/ticker/fiscal-year
    python3 scripts/dry_run_extract.py --url URL --ticker TICKER --fiscal-year YYYY
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.extract_dry_run import PdfBytesInvalid, extract_from_bytes, verify_pdf_bytes

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


def run(url: str, ticker: str, fiscal_year: int) -> dict | None:
    import requests  # local import — keeps module import itself side-effect-free

    print("=" * 78)
    print("DRY-RUN EXTRACTION — read-only, in-memory only")
    print("=" * 78)
    print(f"URL          : {url}")
    print(f"Ticker       : {ticker}")
    print(f"Fiscal year  : {fiscal_year}")
    print()

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
        result = extract_from_bytes(pdf_bytes, ticker=ticker, fiscal_year=fiscal_year)
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


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--url", default=DEFAULT_SABIC_FY2024_URL)
    cli.add_argument("--ticker", default=DEFAULT_TICKER)
    cli.add_argument("--fiscal-year", type=int, default=DEFAULT_FISCAL_YEAR)
    args = cli.parse_args()
    run(args.url, args.ticker, args.fiscal_year)


if __name__ == "__main__":
    main()
