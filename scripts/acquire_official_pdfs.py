#!/usr/bin/env python3
"""
scripts/acquire_official_pdfs.py

Standalone CLI entry point for downloading the officially-verified annual
report PDFs registered in ingestion/load_historical.py's
COMPANY_SOURCE_REGISTRIES. This script does NOT duplicate any acquisition
logic — it is a thin driver around acquire_reports()/acquire_one_report(),
which already implement idempotency, PDF integrity checks, SHA-256, page
counting, and manifest updates. See ingestion/load_historical.py for that
logic.

WHAT THIS SCRIPT DOES:
  1. Runs a connectivity smoke test against a handful of official domains
     (--smoke-test-only, or automatically before the full run unless
     --skip-smoke-test is passed).
  2. For each company in COMPANY_SOURCE_REGISTRIES, acquires ONLY the
     entries whose source_type == "pdf" (report_page and unregistered/
     MISSING years are never attempted — this is enforced by
     acquire_one_report() itself, not re-implemented here).
  3. Prints a final summary table and updates each company's manifest.json
     under data/raw/<company_slug>/ (via acquire_reports()).

WHAT THIS SCRIPT DELIBERATELY DOES NOT DO:
  - No database connection, no SQL, no Neon.
  - No extract_facts()/load_facts() — extraction/loading are out of scope.
  - No modification of any *_SOURCE_REGISTRY, COMPANY_SOURCE_REGISTRIES,
    or any URL — this script only READS the registries.
  - No invented/guessed URLs, no Tier-3 fallback sources.
  - No retry storms — a failed smoke test stops the run (see below).

WHERE THIS RUNS:
  This script has no special dependency on any particular host — it works
  anywhere Python 3 + `requests` can reach the registered domains. It is
  written to be runnable either locally, in a CI job, or on a Render
  worker/shell — see the module docstring in this repo's README/report for
  the current status of *actually running it on Render*, which depends on
  execution/retrieval mechanisms not yet established for this project.

USAGE:
    python3 scripts/acquire_official_pdfs.py                  # smoke test, then full run
    python3 scripts/acquire_official_pdfs.py --smoke-test-only
    python3 scripts/acquire_official_pdfs.py --skip-smoke-test
    python3 scripts/acquire_official_pdfs.py --single company_slug fiscal_year
        # acquire exactly one (company, year) — used as the "smoke test on
        # one real registered PDF" step before committing to the full run.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

# Allow running this script directly (`python3 scripts/acquire_official_pdfs.py`)
# as well as as a module (`python3 -m scripts.acquire_official_pdfs`) by
# ensuring the repo root is on sys.path either way.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.load_historical import (
    COMPANY_SOURCE_REGISTRIES,
    CONFIRMED_COMPANY_TICKERS,
    acquire_reports,
)

# Fixed, small set of official domains used ONLY for the pre-flight
# connectivity smoke test — never used as a download target themselves.
SMOKE_TEST_DOMAINS = [
    "https://www.sabic.com",
    "https://www.yansab.com.sa",
    "https://www.tasnee.com",
]


def run_connectivity_smoke_test() -> bool:
    """HEAD-requests (falling back to a tiny GET if HEAD is rejected)
    against a few official domains. Prints HTTP status, final URL (after
    redirects), content-type, and response size for each. Returns True
    only if ALL three domains responded (any 2xx/3xx/4xx counts as "we
    reached the server" — a 403 from the SITE itself is different from a
    network/proxy-level connection failure, which raises an exception
    instead of returning a status code at all)."""
    import requests

    print("=" * 78)
    print("CONNECTIVITY SMOKE TEST")
    print("=" * 78)
    all_ok = True
    for url in SMOKE_TEST_DOMAINS:
        host = urlparse(url).netloc
        try:
            resp = requests.head(url, timeout=15, allow_redirects=True)
            if resp.status_code == 405:  # some sites reject HEAD
                resp = requests.get(url, timeout=15, allow_redirects=True, stream=True)
            size = len(resp.content) if hasattr(resp, "content") and resp.content else resp.headers.get("Content-Length", "unknown")
            print(f"[OK ] {host}")
            print(f"       HTTP status : {resp.status_code}")
            print(f"       Final URL   : {resp.url}")
            print(f"       Content-Type: {resp.headers.get('Content-Type', 'unknown')}")
            print(f"       Resp. size  : {size}")
        except requests.exceptions.SSLError as e:
            print(f"[FAIL] {host} — TLS error: {e}")
            all_ok = False
        except requests.exceptions.ConnectTimeout as e:
            print(f"[FAIL] {host} — connection TIMEOUT: {e}")
            all_ok = False
        except requests.exceptions.ProxyError as e:
            print(f"[FAIL] {host} — PROXY error (likely egress policy denial, not the site itself): {e}")
            all_ok = False
        except requests.exceptions.ConnectionError as e:
            print(f"[FAIL] {host} — connection error (DNS or network-level, not the site itself): {e}")
            all_ok = False
        except Exception as e:
            print(f"[FAIL] {host} — unexpected error ({type(e).__name__}): {e}")
            all_ok = False
        print("-" * 78)
    print(f"SMOKE TEST RESULT: {'PASS — all domains reachable' if all_ok else 'FAIL — at least one domain unreachable'}")
    print("=" * 78)
    return all_ok


def count_pdf_targets() -> int:
    total = 0
    for registry in COMPANY_SOURCE_REGISTRIES.values():
        total += sum(1 for e in registry.values() if e.get("source_type") == "pdf")
    return total


def diagnose_single(company_slug: str, fiscal_year: int) -> dict:
    """Rich, single-(company, year) diagnostic: makes ONE raw HTTP request
    to capture status code / Content-Type / response size / wall-clock
    timing (information acquire_one_report() doesn't surface), then calls
    the real acquire_one_report() to get the authoritative, verified
    result (PDF integrity, page_count, sha256, file_size_bytes) — the
    exact same function/logic path used for the full 29-file run, so this
    is a genuine test of the real acquisition path, not a separate
    parallel implementation. Prints a full diagnostic block and returns
    a dict of everything captured, for log-based retrieval when run
    somewhere this session can't reach directly (e.g. Render)."""
    import requests

    from ingestion.load_historical import acquire_one_report

    key = (CONFIRMED_COMPANY_TICKERS[company_slug], fiscal_year)
    registry = COMPANY_SOURCE_REGISTRIES[company_slug]
    result = {"company_slug": company_slug, "fiscal_year": fiscal_year}

    print("=" * 78)
    print(f"DIAGNOSTIC SINGLE-FILE TEST: {company_slug} FY{fiscal_year}")
    print("=" * 78)

    if key not in registry:
        print(f"No registry entry for {key} — nothing to test.")
        result["error"] = "no registry entry"
        return result

    entry = registry[key]
    url = entry["source_url"]
    result["url"] = url
    print(f"URL: {url}")

    t0 = time.monotonic()
    try:
        resp = requests.get(url, timeout=30)
        elapsed = time.monotonic() - t0
        result["http_status"] = resp.status_code
        result["content_type"] = resp.headers.get("Content-Type", "unknown")
        result["response_size_bytes"] = len(resp.content)
        result["download_seconds"] = round(elapsed, 2)
        result["starts_with_pdf_magic"] = resp.content[:5] == b"%PDF-"
        print(f"HTTP status      : {resp.status_code}")
        print(f"Content-Type     : {result['content_type']}")
        print(f"Response size    : {result['response_size_bytes']} bytes")
        print(f"Download time    : {result['download_seconds']}s")
        print(f"Starts with %PDF-: {result['starts_with_pdf_magic']}")
        if not result["starts_with_pdf_magic"]:
            print(f"  WARNING: first 200 bytes: {resp.content[:200]!r}")
    except Exception as e:
        elapsed = time.monotonic() - t0
        result["error"] = f"{type(e).__name__}: {e}"
        result["download_seconds"] = round(elapsed, 2)
        print(f"RAW REQUEST FAILED after {result['download_seconds']}s: {result['error']}")
        print("Not proceeding to acquire_one_report() for this entry — raw request already failed.")
        print("=" * 78)
        return result

    # Now run the real, authoritative acquisition path (same function used
    # for the full 29-file run) to get verified integrity/sha256/page_count.
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    record = acquire_one_report(
        ticker=CONFIRMED_COMPANY_TICKERS[company_slug],
        fiscal_year=fiscal_year,
        now_iso=now_iso,
        registry=registry,
        company_slug=company_slug,
    )
    result["acquisition_status"] = record.acquisition_status
    result["page_count"] = record.page_count
    result["sha256"] = record.sha256
    result["file_size_bytes"] = record.file_size_bytes
    result["local_path"] = record.local_path
    result["failure_reason"] = record.failure_reason

    print()
    print(f"acquire_one_report() result:")
    print(f"  acquisition_status : {record.acquisition_status}")
    print(f"  page_count         : {record.page_count}")
    print(f"  sha256             : {record.sha256}")
    print(f"  file_size_bytes    : {record.file_size_bytes}")
    print(f"  local_path         : {record.local_path}")
    if record.failure_reason:
        print(f"  failure_reason     : {record.failure_reason}")
    print("=" * 78)
    return result


def run_full_acquisition() -> dict:
    """Runs acquire_reports() for every company, full FY2015-2024 range.
    acquire_reports()/acquire_one_report() already skip report_page and
    unregistered (MISSING) years without any network attempt — nothing
    extra is needed here to enforce that."""
    all_manifests = {}
    for slug, registry in COMPANY_SOURCE_REGISTRIES.items():
        ticker = CONFIRMED_COMPANY_TICKERS[slug]
        pdf_count = sum(1 for e in registry.values() if e.get("source_type") == "pdf")
        print(f"--- {slug} ({ticker}) — {pdf_count} pdf target(s) ---")
        manifest = acquire_reports(
            ticker=ticker,
            fiscal_years=list(range(2015, 2025)),
            registry=registry,
            company_slug=slug,
        )
        all_manifests[slug] = manifest
        for fy, rec in sorted(manifest["records"].items(), key=lambda x: int(x[0])):
            if rec.get("source_type") == "pdf":
                status = rec["acquisition_status"]
                extra = f" | {rec.get('failure_reason', '')[:80]}" if status != "AVAILABLE" else f" | sha256={rec.get('sha256')}"
                print(f"  FY{fy}: {status}{extra}")
    return all_manifests


def print_summary(manifests: dict) -> None:
    available = failed = unavailable = 0
    for manifest in manifests.values():
        for rec in manifest["records"].values():
            if rec.get("source_type") != "pdf":
                continue
            status = rec["acquisition_status"]
            if status == "AVAILABLE":
                available += 1
            elif status == "FAILED":
                failed += 1
            elif status == "SOURCE_DISCOVERED_BUT_FILE_UNAVAILABLE":
                unavailable += 1
    total_targeted = count_pdf_targets()
    print()
    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"Total targeted        = {total_targeted}")
    print(f"Downloaded successfully (AVAILABLE) = {available}")
    print(f"Failed                 = {failed}")
    print(f"Unavailable            = {unavailable}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke-test-only", action="store_true")
    parser.add_argument("--skip-smoke-test", action="store_true")
    parser.add_argument("--single", nargs=2, metavar=("COMPANY_SLUG", "FISCAL_YEAR"))
    parser.add_argument(
        "--diagnose", nargs=2, metavar=("COMPANY_SLUG", "FISCAL_YEAR"),
        help="Rich single-file diagnostic: HTTP status, Content-Type, size, "
             "timing, PDF magic-byte check, then full acquire_one_report() "
             "(integrity, page_count, sha256). No smoke test, no full run.",
    )
    args = parser.parse_args()

    if args.diagnose:
        slug, fy = args.diagnose
        fy = int(fy)
        if slug not in COMPANY_SOURCE_REGISTRIES:
            print(f"Unknown company_slug: {slug!r}. Known: {sorted(COMPANY_SOURCE_REGISTRIES)}")
            sys.exit(1)
        diagnose_single(slug, fy)
        return

    if args.single:
        slug, fy = args.single
        fy = int(fy)
        if slug not in COMPANY_SOURCE_REGISTRIES:
            print(f"Unknown company_slug: {slug!r}. Known: {sorted(COMPANY_SOURCE_REGISTRIES)}")
            sys.exit(1)
        ticker = CONFIRMED_COMPANY_TICKERS[slug]
        manifest = acquire_reports(
            ticker=ticker, fiscal_years=[fy],
            registry=COMPANY_SOURCE_REGISTRIES[slug], company_slug=slug,
        )
        rec = manifest["records"][str(fy)]
        print(f"{slug} FY{fy}: {rec['acquisition_status']}")
        if rec.get("failure_reason"):
            print(f"  reason: {rec['failure_reason']}")
        return

    if not args.skip_smoke_test:
        ok = run_connectivity_smoke_test()
        if not ok:
            print()
            print("Smoke test FAILED. Per instructions, stopping here — not attempting")
            print("the full 29-PDF run, and not retrying. Registry left untouched.")
            sys.exit(2)

    if args.smoke_test_only:
        return

    manifests = run_full_acquisition()
    print_summary(manifests)


if __name__ == "__main__":
    main()
