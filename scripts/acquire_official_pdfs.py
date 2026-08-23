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
import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

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


# Diagnostic-only candidate "report index" pages, discovered via WebSearch.
# These are used ONLY for read-only HTTP-behavior diagnostics — they are
# NEVER added to any *_SOURCE_REGISTRY and are never used as a download
# target for acquire_one_report(). Extending this dict does not register a
# new source; it only gives diagnose_http_investigation() a second, known
# official URL to test alongside the registered PDF URL.
DIAGNOSTIC_REPORT_PAGE_CANDIDATES = {
    "sabic": "https://www.sabic.com/en/investors/performance-financial-highlights/annual-report",
}

# A normal desktop-browser User-Agent + Accept headers, used only to see
# whether the direct PDF URL's HTTP 500 is sensitive to request headers
# (e.g. bot/WAF filtering) — never used to disguise automated bulk
# downloading, and never combined with retries.
BROWSER_LIKE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Fixed control PDF used ONLY to distinguish "SABIC-specific rejection"
# from "Render/network cannot fetch ANY PDF at all". Verified via WebSearch
# as a real, current, official U.S. government PDF (SEC EDGAR Filer Manual,
# Volume II) — not guessed/pattern-generated, unrelated to any target
# company, never added to any *_SOURCE_REGISTRY, never downloaded to disk.
CONTROL_TEST_US_PDF_URL = "https://www.sec.gov/files/edgar/filermanual/efmvol2-c2.pdf"

# Bot-protection / WAF / CDN signal hunting is limited to a fixed, small set
# of well-known header names and body keywords — no brute forcing, no
# expanding this list based on trial and error against the live site.
WAF_SIGNAL_HEADERS = (
    "Server", "Via", "X-Cache", "X-Cache-Hits", "CF-RAY", "cf-mitigated",
    "X-Akamai-Transformed", "X-Amz-Cf-Id", "X-Sucuri-ID", "X-Sucuri-Cache",
    "X-Request-Id", "X-Correlation-Id", "Set-Cookie",
)
WAF_SIGNAL_BODY_KEYWORDS = (
    "cloudflare", "akamai", "incapsula", "sucuri", "waf", "captcha",
    "access denied", "forbidden", "blocked", "bot detection",
    "internal server error", "500", "error", "not found", "redirect",
)


def _summarize_response(label: str, resp, elapsed_seconds: float | None = None) -> dict:
    """Common read-only summary for a single requests.Response: status,
    content-type, size, redirect history, final URL, PDF magic bytes, and
    (for HTML bodies only) a short truncated snippet + WAF/CDN header and
    keyword signals. Never writes the body to disk. elapsed_seconds is
    optional wall-clock timing for the request, supplied by the caller
    (this function performs no I/O itself, so it can't time anything)."""
    content_type = resp.headers.get("Content-Type", "unknown")
    is_html = "html" in content_type.lower()
    summary = {
        "label": label,
        "http_status": resp.status_code,
        "content_type": content_type,
        "response_size_bytes": len(resp.content),
        "final_url": resp.url,
        "redirect_history": [(r.status_code, r.url) for r in resp.history],
        "redirect_count": len(resp.history),
        "starts_with_pdf_magic": resp.content[:5] == b"%PDF-",
    }
    print(f"  [{label}]")
    print(f"    HTTP status      : {summary['http_status']}")
    print(f"    Content-Type     : {content_type}")
    print(f"    Response size    : {summary['response_size_bytes']} bytes")
    if elapsed_seconds is not None:
        summary["elapsed_seconds"] = round(elapsed_seconds, 2)
        print(f"    Elapsed time     : {summary['elapsed_seconds']}s")
    print(f"    Redirect count   : {summary['redirect_count']}")
    print(f"    Redirect history : {summary['redirect_history'] or '(none)'}")
    print(f"    Final URL        : {summary['final_url']}")
    print(f"    Starts with %PDF-: {summary['starts_with_pdf_magic']}")

    if is_html:
        text = resp.text
        title_start = text.lower().find("<title>")
        title_end = text.lower().find("</title>")
        title = text[title_start + 7:title_end].strip() if 0 <= title_start < title_end else "(no <title> found)"
        summary["html_title"] = title
        print(f"    HTML <title>     : {title!r}")

        present_waf_headers = {h: resp.headers[h] for h in WAF_SIGNAL_HEADERS if h in resp.headers}
        summary["waf_signal_headers"] = present_waf_headers
        print(f"    WAF/CDN headers  : {present_waf_headers or '(none of the checked header names present)'}")

        lowered = text.lower()
        found_keywords = [kw for kw in WAF_SIGNAL_BODY_KEYWORDS if kw in lowered]
        summary["waf_signal_keywords_in_body"] = found_keywords
        print(f"    Body keywords    : {found_keywords or '(none of the checked keywords found)'}")

        snippet = text[:500].replace("\n", " ").replace("\r", " ")
        summary["html_snippet_first_500_chars"] = snippet
        print(f"    Body snippet(500): {snippet!r}")
    return summary


def _timed_get(url: str, **kwargs) -> tuple[object | None, float, str | None]:
    """Single GET with no retries, returning (response_or_None, elapsed_seconds,
    error_string_or_None). Pure timing/error wrapper — no summarization."""
    import requests

    t0 = time.monotonic()
    try:
        resp = requests.get(url, **kwargs)
        return resp, time.monotonic() - t0, None
    except Exception as e:
        return None, time.monotonic() - t0, f"{type(e).__name__}: {e}"


def diagnose_http_investigation(company_slug: str, fiscal_year: int) -> dict:
    """Investigates WHY a registered PDF URL returns something other than a
    real PDF (e.g. HTTP 500 / text/html), without modifying the registry,
    without retries, without curl, and without saving any body to disk.
    Makes at most 4 read-only GET requests total, reported as three named
    sections:
      CONTROL TEST — U.S. PDF
        A fixed, unrelated, reputable U.S. government PDF (see
        CONTROL_TEST_US_PDF_URL) — establishes whether Render can fetch
        ANY real PDF at all, independent of SABIC entirely.
      SABIC FY2024 PDF
        1. The exact registered URL, current requests config (no custom
           headers) — matches what acquire_one_report() itself sends.
        2. The exact registered URL, with a normal browser User-Agent/Accept.
      SABIC FY2024 official report/index page
        3. (If a diagnostic report-index page is known for this company)
           that page, to see whether the *page* is reachable even if the
           direct PDF link is not.
    Does NOT call acquire_one_report() — this is HTTP-behavior
    investigation only, separate from the authoritative acquisition path.
    """
    registry = COMPANY_SOURCE_REGISTRIES[company_slug]
    ticker = CONFIRMED_COMPANY_TICKERS[company_slug]
    key = (ticker, fiscal_year)
    result = {"company_slug": company_slug, "fiscal_year": fiscal_year}

    print("=" * 78)
    print(f"HTTP INVESTIGATION: {company_slug} FY{fiscal_year} — why not a real PDF?")
    print("=" * 78)

    if key not in registry:
        print(f"No registry entry for {key} — nothing to test.")
        result["error"] = "no registry entry"
        return result

    url = registry[key]["source_url"]
    result["url"] = url
    print(f"Registered URL: {url}")
    print()

    # --- CONTROL TEST — U.S. PDF --------------------------------------------
    print("CONTROL TEST — U.S. PDF")
    print(f"  {CONTROL_TEST_US_PDF_URL}")
    resp, elapsed, err = _timed_get(CONTROL_TEST_US_PDF_URL, timeout=30, headers=BROWSER_LIKE_HEADERS)
    if resp is not None:
        result["control_test_us_pdf"] = _summarize_response("U.S. control PDF", resp, elapsed)
    else:
        result["control_test_us_pdf"] = {"error": err, "elapsed_seconds": round(elapsed, 2)}
        print(f"  REQUEST FAILED after {round(elapsed, 2)}s: {err}")
    print()

    # --- SABIC FY2024 PDF ----------------------------------------------------
    print("SABIC FY2024 PDF")
    print("  TEST 1 — exact URL, current requests config (no custom headers):")
    resp, elapsed, err = _timed_get(url, timeout=30)
    if resp is not None:
        result["test_1_default_headers"] = _summarize_response("default headers", resp, elapsed)
    else:
        result["test_1_default_headers"] = {"error": err, "elapsed_seconds": round(elapsed, 2)}
        print(f"  REQUEST FAILED after {round(elapsed, 2)}s: {err}")
    print()

    print("  TEST 2 — exact URL, normal browser-like User-Agent + Accept headers:")
    resp, elapsed, err = _timed_get(url, timeout=30, headers=BROWSER_LIKE_HEADERS)
    if resp is not None:
        result["test_2_browser_headers"] = _summarize_response("browser-like headers", resp, elapsed)
    else:
        result["test_2_browser_headers"] = {"error": err, "elapsed_seconds": round(elapsed, 2)}
        print(f"  REQUEST FAILED after {round(elapsed, 2)}s: {err}")
    print()

    # --- SABIC FY2024 official report/index page ------------------------------
    print("SABIC FY2024 official report/index page")
    report_page_url = DIAGNOSTIC_REPORT_PAGE_CANDIDATES.get(company_slug)
    if report_page_url:
        print(f"  (diagnostic only, NOT registered) {report_page_url}")
        resp, elapsed, err = _timed_get(report_page_url, timeout=30, headers=BROWSER_LIKE_HEADERS)
        if resp is not None:
            result["test_3_report_page"] = _summarize_response("report-index page", resp, elapsed)
        else:
            result["test_3_report_page"] = {"error": err, "elapsed_seconds": round(elapsed, 2)}
            print(f"  REQUEST FAILED after {round(elapsed, 2)}s: {err}")
    else:
        print("  No diagnostic report-index page known for this company; skipped.")
        result["test_3_report_page"] = None

    print()
    print("=" * 78)
    print("CONCLUSIONS")
    print("=" * 78)
    ctrl = result.get("control_test_us_pdf", {})
    t1 = result.get("test_1_default_headers", {})
    t2 = result.get("test_2_browser_headers", {})

    if ctrl.get("starts_with_pdf_magic"):
        print("A) Render CAN download real PDF bytes generally — control test succeeded "
              f"(HTTP {ctrl['http_status']}, {ctrl['response_size_bytes']} bytes, "
              f"{ctrl.get('elapsed_seconds')}s).")
    elif "http_status" in ctrl:
        print(f"A) Render reached the U.S. control host but did NOT get a PDF back "
              f"(HTTP {ctrl['http_status']}, {ctrl['content_type']}) — possible broader issue.")
    else:
        print(f"A) Render could not even reach the U.S. control host: {ctrl.get('error')} "
              "— points to (C) a broader Render/network issue, not something SABIC-specific.")

    sabic_pdf_ok = t1.get("starts_with_pdf_magic") or t2.get("starts_with_pdf_magic")
    if sabic_pdf_ok:
        print("B) SABIC direct PDF URL returned real PDF bytes.")
    elif "http_status" in t1 or "http_status" in t2:
        statuses = {t1.get("http_status"), t2.get("http_status")} - {None}
        print(f"B) SABIC direct PDF URL did NOT return PDF bytes (HTTP {sorted(statuses)}).")
        if ctrl.get("starts_with_pdf_magic"):
            print("   -> Control PDF succeeded but SABIC PDF did not: this looks "
                  "SABIC-specific, not a general Render/network problem.")
    else:
        print(f"B) Could not reach the SABIC direct PDF URL at all: "
              f"{t1.get('error') or t2.get('error')}")

    if t1.get("http_status") != t2.get("http_status") and "http_status" in t1 and "http_status" in t2:
        print("   -> Header-sensitive response: default vs. browser-like headers got a "
              "DIFFERENT HTTP status. Possible bot/WAF filtering on request headers.")

    t3 = result.get("test_3_report_page")
    if t3 and "http_status" in t3:
        print(f"Report-index page response: HTTP {t3['http_status']} at final URL {t3['final_url']}")
    print("=" * 78)
    return result


# --- Report-page link discovery ---------------------------------------------
# Investigates whether a company's official report-index PAGE (which may
# return HTTP 200 even when the direct PDF URL is blocked) exposes a
# different download/CDN URL for the target fiscal year's report. Uses only
# the standard library (html.parser + re) — no new dependency added to
# requirements.txt. Diagnostic only: makes exactly ONE GET request, never
# downloads a PDF, never saves the HTML body to disk, never modifies any
# registry.

DOCUMENT_LINK_KEYWORDS = ("pdf", "annual", "report", "download", "document")

# Looks for JS variables/fetch calls/API-style paths that reference annual
# reports — a small, fixed set of patterns, not an open-ended crawler.
JS_REFERENCE_PATTERN = re.compile(
    r'''(?:var|let|const)\s+\w+\s*=\s*["']([^"']*(?:annual|report|pdf)[^"']*)["']'''
    r'''|fetch\(\s*["']([^"']*(?:annual|report|pdf)[^"']*)["']'''
    r'''|["'](/api/[^"']*(?:annual|report)[^"']*)["']''',
    re.IGNORECASE,
)
# data-* attributes whose value looks like a document/download URL.
DATA_ATTRIBUTE_PATTERN = re.compile(
    r'''data-[\w-]+\s*=\s*["']([^"']*(?:pdf|download|document)[^"']*)["']''',
    re.IGNORECASE,
)


class _ReportLinkHTMLParser(HTMLParser):
    """Collects <a href>, <iframe src>, <embed src>, <object data> candidates
    plus a short window of surrounding text as context. Pure parsing, no
    network I/O, no file writes."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.candidates: list[dict] = []
        self._recent_text = ""
        self._capturing_anchor: dict | None = None

    def handle_starttag(self, tag, attrs):
        # Each candidate's "preceding" context is the text seen since the
        # PREVIOUS candidate-producing tag only (not the whole document) —
        # the buffer is reset right after being read, so an unrelated link
        # later in the page never inherits an earlier link's "2024"/"report"
        # wording just because it appeared somewhere earlier on the page.
        attrs_dict = dict(attrs)
        if tag == "a" and attrs_dict.get("href"):
            self._capturing_anchor = {
                "href": attrs_dict["href"], "text": "",
                "preceding": self._recent_text[-150:],
            }
            self._recent_text = ""
        elif tag == "iframe" and attrs_dict.get("src"):
            self.candidates.append({
                "source": "iframe_src", "url": attrs_dict["src"],
                "context": self._recent_text[-150:],
            })
            self._recent_text = ""
        elif tag == "embed" and attrs_dict.get("src"):
            self.candidates.append({
                "source": "embed_src", "url": attrs_dict["src"],
                "context": self._recent_text[-150:],
            })
            self._recent_text = ""
        elif tag == "object" and attrs_dict.get("data"):
            self.candidates.append({
                "source": "object_data", "url": attrs_dict["data"],
                "context": self._recent_text[-150:],
            })
            self._recent_text = ""

    def handle_endtag(self, tag):
        if tag == "a" and self._capturing_anchor is not None:
            anchor = self._capturing_anchor
            context = f"{anchor['preceding']} [LINK TEXT: {anchor['text'].strip()}]"
            self.candidates.append({"source": "a_href", "url": anchor["href"], "context": context})
            self._capturing_anchor = None
            self._recent_text = ""

    def handle_data(self, data):
        if self._capturing_anchor is not None:
            self._capturing_anchor["text"] += data
        self._recent_text = (self._recent_text + data)[-500:]


def extract_report_link_candidates(html_text: str, base_url: str) -> list[dict]:
    """Pure function: parses html_text (already-fetched HTML, no I/O here)
    and returns a list of {source, url, resolved_url, context} dicts —
    hrefs/iframe/embed/object candidates plus JS-reference and data-attribute
    matches found via regex over the raw HTML. Relative URLs are resolved
    against base_url."""
    parser = _ReportLinkHTMLParser()
    parser.feed(html_text)
    candidates = list(parser.candidates)

    for match in JS_REFERENCE_PATTERN.finditer(html_text):
        url = next((g for g in match.groups() if g), None)
        if not url:
            continue
        start, end = max(0, match.start() - 60), min(len(html_text), match.end() + 60)
        candidates.append({"source": "js_reference", "url": url, "context": html_text[start:end]})

    for match in DATA_ATTRIBUTE_PATTERN.finditer(html_text):
        url = match.group(1)
        start, end = max(0, match.start() - 60), min(len(html_text), match.end() + 60)
        candidates.append({"source": "data_attribute", "url": url, "context": html_text[start:end]})

    for c in candidates:
        c["resolved_url"] = urljoin(base_url, c["url"])
    return candidates


def _classify_candidate(url: str, context: str, target_fiscal_year: int) -> str:
    haystack = f"{url} {context}".lower()
    has_year = str(target_fiscal_year) in haystack
    has_doc_keyword = any(kw in haystack for kw in DOCUMENT_LINK_KEYWORDS)
    if has_year and (".pdf" in url.lower() or has_doc_keyword):
        return "VERIFIED_FY_CANDIDATE"
    if has_doc_keyword:
        return "POSSIBLE_CANDIDATE"
    return "UNRELATED"


def diagnose_report_page_links(company_slug: str, target_fiscal_year: int) -> dict:
    """Fetches a company's known official report-index page EXACTLY ONCE
    and inspects its HTML for how the target fiscal year's report is
    actually linked (direct href, iframe/embed/object, JS reference, or a
    data-* attribute), classifying each candidate as a VERIFIED FYnnnn
    candidate, a POSSIBLE candidate, or an UNRELATED report link. Diagnostic
    only — never downloads a PDF, never saves the HTML body to disk, never
    touches any *_SOURCE_REGISTRY."""
    report_page_url = DIAGNOSTIC_REPORT_PAGE_CANDIDATES.get(company_slug)
    result = {
        "company_slug": company_slug,
        "target_fiscal_year": target_fiscal_year,
        "report_page_url": report_page_url,
    }

    print("=" * 78)
    print(f"REPORT-PAGE LINK DISCOVERY: {company_slug} — target FY{target_fiscal_year}")
    print("=" * 78)

    if not report_page_url:
        print("No diagnostic report-index page known for this company; nothing to test.")
        result["error"] = "no report page url"
        return result

    print(f"Fetching (exactly ONE GET request): {report_page_url}")
    resp, elapsed, err = _timed_get(report_page_url, timeout=30, headers=BROWSER_LIKE_HEADERS)
    result["elapsed_seconds"] = round(elapsed, 2)
    if resp is None:
        result["error"] = err
        print(f"REQUEST FAILED after {result['elapsed_seconds']}s: {err}")
        print("=" * 78)
        return result

    content_type = resp.headers.get("Content-Type", "unknown")
    result["http_status"] = resp.status_code
    result["content_type"] = content_type
    result["response_size_bytes"] = len(resp.content)
    print(f"HTTP status  : {resp.status_code}")
    print(f"Content-Type : {content_type}")
    print(f"Response size: {result['response_size_bytes']} bytes")
    print(f"Elapsed      : {result['elapsed_seconds']}s")

    if resp.status_code != 200 or "html" not in content_type.lower():
        print("Response is not a 200 HTML page — nothing to parse.")
        result["candidates"] = []
        print("=" * 78)
        return result

    candidates = extract_report_link_candidates(resp.text, resp.url)
    for c in candidates:
        c["classification"] = _classify_candidate(c["url"], c["context"], target_fiscal_year)
    result["candidates"] = candidates

    sections = (
        (f"VERIFIED FY{target_fiscal_year} candidate", "VERIFIED_FY_CANDIDATE"),
        ("POSSIBLE candidate", "POSSIBLE_CANDIDATE"),
        ("UNRELATED report link", "UNRELATED"),
    )
    for label, key in sections:
        matches = [c for c in candidates if c["classification"] == key]
        print()
        print(f"{label} ({len(matches)}):")
        if not matches:
            print("  (none found)")
        for c in matches:
            print(f"  [{c['source']}] {c['resolved_url']}")
            if c["resolved_url"] != c["url"]:
                print(f"      raw (relative): {c['url']}")
            print(f"      context: {c['context'].strip()[:200]!r}")

    print()
    print("=" * 78)
    return result


# --- Report-endpoint discovery (fetch/axios/XHR, JSON blobs, external JS) --
# Goes further than diagnose_report_page_links(): also inspects inline
# <script> contents, external <script src> files (fetched read-only, only
# when their URL itself looks report-related, capped at 2), and JSON blobs
# for fetch()/axios()/XMLHttpRequest calls and documentUrl/fileUrl-style
# fields. Still: no Selenium/Playwright/browser automation, no new
# dependency, no PDF download, no HTML/JS saved to disk, no registry or
# acquisition-logic changes. Uses only the standard library.

ENDPOINT_KEYWORDS = (
    "annual", "report", "financial", "document", "download", "publication", "investor",
)

EXTERNAL_SCRIPT_SRC_PATTERN = re.compile(r'<script[^>]*\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)
INLINE_SCRIPT_PATTERN = re.compile(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL)

# fetch(...) / axios(...)/axios.get(...) / XMLHttpRequest.open('GET', ...)
CALL_SITE_PATTERN = re.compile(
    r'''(?:fetch|axios(?:\.\w+)?)\(\s*["']([^"']+)["']'''
    r'''|\.open\(\s*["']GET["']\s*,\s*["']([^"']+)["']''',
    re.IGNORECASE,
)
# Any quoted string (URL-shaped) containing a report/API keyword.
GENERIC_ENDPOINT_URL_PATTERN = re.compile(
    r'''["']((?:https?://|/)[^"']*(?:annual|report|financial|document|download|publication|investor|/api/)[^"']*)["']''',
    re.IGNORECASE,
)
# Single-level (no nested braces) JSON-ish object containing a document-url field.
JSON_OBJECT_WITH_DOC_FIELD_PATTERN = re.compile(
    r'\{[^{}]*"(?:documentUrl|fileUrl|pdfUrl|reportUrl)"[^{}]*\}', re.IGNORECASE,
)
JSON_DOC_FIELD_KEYS = ("documentUrl", "fileUrl", "pdfUrl", "reportUrl")


def _parse_json_doc_blob(blob: str, source_label: str) -> dict | None:
    """Tries a strict json.loads() first; falls back to regex extraction for
    the common real-world case of a JS object literal that isn't quite
    valid JSON (unquoted keys, trailing commas). Returns a candidate dict
    or None if no document-url field is found either way."""
    try:
        obj = json.loads(blob)
    except Exception:
        obj = None
    if isinstance(obj, dict):
        for key in JSON_DOC_FIELD_KEYS:
            value = obj.get(key)
            if isinstance(value, str):
                year = obj.get("year")
                reason = f'JSON object field "{key}"'
                if year is not None:
                    reason += f" (year field = {year!r})"
                return {
                    "source": source_label, "raw_value": value, "reason": reason,
                    "context": blob[:200], "_json_year": year,
                }
        return None

    key_match = re.search(r'"(documentUrl|fileUrl|pdfUrl|reportUrl)"\s*:\s*"([^"]+)"', blob, re.IGNORECASE)
    if not key_match:
        return None
    year_match = re.search(r'"year"\s*:\s*"?(\d{4})"?', blob, re.IGNORECASE)
    reason = f'JSON-like object field "{key_match.group(1)}" (regex fallback — strict JSON parse failed)'
    if year_match:
        reason += f" (year field = {year_match.group(1)!r})"
    return {
        "source": source_label, "raw_value": key_match.group(2), "reason": reason,
        "context": blob[:200], "_json_year": year_match.group(1) if year_match else None,
    }


def _extract_js_endpoint_candidates(js_text: str, source_label: str) -> list[dict]:
    """Pure function, no I/O: scans one block of JavaScript text (inline or
    externally-fetched) for fetch/axios/XHR call sites, generic keyword URL
    string literals, and JSON document-field blobs."""
    found = []
    for match in CALL_SITE_PATTERN.finditer(js_text):
        url = next((g for g in match.groups() if g), None)
        if not url:
            continue
        start, end = max(0, match.start() - 60), min(len(js_text), match.end() + 60)
        found.append({
            "source": source_label, "raw_value": url,
            "reason": "found inside a fetch()/axios()/XMLHttpRequest.open() call",
            "context": js_text[start:end],
        })
    for match in GENERIC_ENDPOINT_URL_PATTERN.finditer(js_text):
        url = match.group(1)
        start, end = max(0, match.start() - 60), min(len(js_text), match.end() + 60)
        found.append({
            "source": source_label, "raw_value": url,
            "reason": "string literal containing a report/API keyword",
            "context": js_text[start:end],
        })
    for match in JSON_OBJECT_WITH_DOC_FIELD_PATTERN.finditer(js_text):
        entry = _parse_json_doc_blob(match.group(0), source_label)
        if entry:
            found.append(entry)
    return found


def _looks_like_document_url(url: str) -> bool:
    """True only for a URL that actually points AT a document/file, not a
    navigational index page. Checked against the URL's path only (query
    strings like "?v=..." stripped), using the two patterns every real
    report link seen across every registry and every live diagnostic run
    in this codebase has actually used: a .pdf extension, or SABIC's own
    document CDN path (/Images/...). A plain nav page merely mentioning
    "report"/"investor"/"publications" in its path (e.g. /en/investors,
    /en/reports, /en/newsandmedia/reports) does NOT qualify — that keyword-
    only false positive was exactly what previously flooded the POSSIBLE
    section with ordinary navigation links instead of real documents."""
    path = urlparse(url).path.lower()
    return path.endswith(".pdf") or "/images/" in path


def _classify_endpoint_candidate(raw_value: str, context: str, target_fiscal_year: int,
                                  json_year=None) -> str:
    """HIGH_CONFIDENCE / POSSIBLE / UNRELATED.

    HIGH_CONFIDENCE requires the candidate to be BOTH a real document/API
    endpoint AND tied to the target fiscal year (matching JSON "year"
    field, or the year appearing in the URL/context for a document URL,
    or an /api/ path carrying a report/document keyword).

    POSSIBLE covers a real document URL or /api/ path that isn't (yet)
    confirmed to be the target year — e.g. a genuine, current annual-
    report PDF for a DIFFERENT year, which is exactly what should surface
    here rather than being lost in a wall of keyword-only navigation
    links.

    A bare keyword hit on a URL that is neither a document URL nor an
    /api/ path (ordinary navigation: /en/investors, /en/reports, ...) is
    UNRELATED — it is not a report/document link just because the word
    "report" or "investor" appears somewhere in its path."""
    if json_year is not None and str(json_year) == str(target_fiscal_year):
        return "HIGH_CONFIDENCE"
    haystack = f"{raw_value} {context}".lower()
    has_year = str(target_fiscal_year) in haystack
    is_api_path = "/api/" in raw_value.lower()
    is_document_url = _looks_like_document_url(raw_value)
    keyword_hits = [k for k in ENDPOINT_KEYWORDS if k in haystack]

    if is_api_path and keyword_hits:
        return "HIGH_CONFIDENCE"
    if is_document_url and has_year:
        return "HIGH_CONFIDENCE"
    if is_document_url or is_api_path:
        return "POSSIBLE"
    return "UNRELATED"


def diagnose_report_endpoints(company_slug: str, target_fiscal_year: int,
                               report_page_url: str | None = None) -> dict:
    """Extends diagnose_report_page_links() with inline-script, external-JS,
    and JSON-blob endpoint discovery, then tests (read-only, max 3 GETs,
    no retries) only the HIGH-CONFIDENCE endpoints found. Never downloads
    or saves a PDF, never saves HTML/JS to disk, never modifies any
    registry or acquisition logic.

    report_page_url: optional override for the page to inspect, used for
    THIS call only — e.g. to point at a different official page (such as
    an archive/publications index) for the same company without touching
    DIAGNOSTIC_REPORT_PAGE_CANDIDATES or any *_SOURCE_REGISTRY. When
    omitted (the default, used by every existing caller), behavior is
    byte-for-byte unchanged: the company's registered diagnostic page is
    used, exactly as before this parameter existed."""
    if report_page_url is None:
        report_page_url = DIAGNOSTIC_REPORT_PAGE_CANDIDATES.get(company_slug)
    result = {
        "company_slug": company_slug, "target_fiscal_year": target_fiscal_year,
        "report_page_url": report_page_url,
    }

    print("=" * 78)
    print(f"REPORT ENDPOINT DISCOVERY: {company_slug} — target FY{target_fiscal_year}")
    print("=" * 78)

    if not report_page_url:
        print("No diagnostic report-index page known for this company; nothing to test.")
        result["error"] = "no report page url"
        return result

    print(f"Fetching report-index page (1 GET request): {report_page_url}")
    resp, elapsed, err = _timed_get(report_page_url, timeout=30, headers=BROWSER_LIKE_HEADERS)
    result["page_elapsed_seconds"] = round(elapsed, 2)
    if resp is None:
        result["error"] = err
        print(f"REQUEST FAILED after {result['page_elapsed_seconds']}s: {err}")
        print("=" * 78)
        return result

    content_type = resp.headers.get("Content-Type", "unknown")
    result["page_http_status"] = resp.status_code
    result["page_content_type"] = content_type
    print(f"HTTP status: {resp.status_code}  Content-Type: {content_type}  "
          f"elapsed: {result['page_elapsed_seconds']}s")

    if resp.status_code != 200 or "html" not in content_type.lower():
        print("Response is not a 200 HTML page — nothing to parse.")
        result["candidates"] = []
        print("=" * 78)
        return result

    html_text = resp.text
    base_url = resp.url
    candidates: list[dict] = []

    # HTML-level candidates already covered by extract_report_link_candidates
    # (href / iframe / embed / object / data-attribute / inline js_reference).
    source_label_map = {
        "a_href": "HTML", "iframe_src": "HTML", "embed_src": "HTML", "object_data": "HTML",
        "js_reference": "inline JS", "data_attribute": "data attribute",
    }
    for c in extract_report_link_candidates(html_text, base_url):
        candidates.append({
            "source": source_label_map.get(c["source"], c["source"]),
            "raw_value": c["url"], "resolved_url": c["resolved_url"],
            "reason": f"matched via {c['source']} pattern", "context": c["context"],
        })

    # Inline <script>...</script> contents.
    for script_match in INLINE_SCRIPT_PATTERN.finditer(html_text):
        for entry in _extract_js_endpoint_candidates(script_match.group(1), "inline JS"):
            entry["resolved_url"] = urljoin(base_url, entry["raw_value"])
            candidates.append(entry)

    # External <script src="..."> files: always noted; only FETCHED
    # (read-only, no save) when the src URL either looks report-related by
    # keyword OR is served from the SAME ORIGIN as the report page itself —
    # a generic bundle name (main.js, templates.js, libs.js, ...) carries no
    # report keyword in its filename, but same-origin JS is still official
    # first-party code, unlike any third-party/external-domain script (which
    # remains excluded either way). Still capped at 2 files fetched.
    external_js_fetched = 0
    for src_match in EXTERNAL_SCRIPT_SRC_PATTERN.finditer(html_text):
        src = src_match.group(1)
        resolved_src = urljoin(base_url, src)
        candidates.append({
            "source": "HTML", "raw_value": src, "resolved_url": resolved_src,
            "reason": "external <script src> file reference", "context": src,
        })
        same_origin = urlparse(resolved_src).netloc == urlparse(base_url).netloc
        looks_relevant = same_origin or any(kw in src.lower() for kw in ENDPOINT_KEYWORDS)
        if looks_relevant and external_js_fetched < 2:
            print(f"Fetching external JS file referenced by the page (read-only, not saved): {resolved_src}")
            js_resp, js_elapsed, js_err = _timed_get(resolved_src, timeout=30, headers=BROWSER_LIKE_HEADERS)
            external_js_fetched += 1
            if js_resp is not None and js_resp.status_code == 200:
                for entry in _extract_js_endpoint_candidates(js_resp.text, "external JS"):
                    entry["resolved_url"] = urljoin(resolved_src, entry["raw_value"])
                    candidates.append(entry)
            elif js_resp is not None:
                print(f"  external JS fetch returned HTTP {js_resp.status_code} — skipped analysis")
            else:
                print(f"  external JS fetch FAILED: {js_err}")
    result["external_js_files_fetched"] = external_js_fetched

    for c in candidates:
        c["classification"] = _classify_endpoint_candidate(
            c["raw_value"], c["context"], target_fiscal_year, c.get("_json_year"),
        )

    print()
    for c in candidates:
        print("CANDIDATE ENDPOINT")
        print("------------------")
        print(f"source: {c['source']}")
        print(f"raw value: {c['raw_value']}")
        print(f"resolved URL: {c['resolved_url']}")
        print(f"reason: {c['reason']}")
        print(f"context: {c['context'].strip()[:200]!r}")
        print()

    sections = (
        ("HIGH-CONFIDENCE REPORT ENDPOINT", "HIGH_CONFIDENCE"),
        ("POSSIBLE REPORT ENDPOINT", "POSSIBLE"),
        ("UNRELATED ENDPOINT", "UNRELATED"),
    )
    for label, key in sections:
        matches = [c for c in candidates if c["classification"] == key]
        print(f"{label} ({len(matches)}):")
        if not matches:
            print("  (none found)")
        for c in matches:
            print(f"  [{c['source']}] {c['resolved_url']}  — {c['reason']}")
        print()
    result["candidates"] = candidates

    # --- Network test phase: HIGH-CONFIDENCE endpoints only, max 3 GETs ----
    seen_urls = set()
    high_confidence = []
    for c in candidates:
        if c["classification"] == "HIGH_CONFIDENCE" and c["resolved_url"] not in seen_urls:
            high_confidence.append(c)
            seen_urls.add(c["resolved_url"])
    to_test = high_confidence[:3]
    if len(high_confidence) > 3:
        skipped = [c["resolved_url"] for c in high_confidence[3:]]
        print(f"NOTE: {len(high_confidence)} HIGH-CONFIDENCE endpoints found; testing only the "
              f"first 3 per the fixed cap. Not tested: {skipped}")

    print("=" * 78)
    print(f"TESTING {len(to_test)} HIGH-CONFIDENCE ENDPOINT(S) (max 3, read-only, no retries)")
    print("=" * 78)

    endpoint_test_results = []
    for c in to_test:
        print(f"Testing: {c['resolved_url']}")
        resp2, elapsed2, err2 = _timed_get(c["resolved_url"], timeout=30, headers=BROWSER_LIKE_HEADERS)
        test_result = {"url": c["resolved_url"], "elapsed_seconds": round(elapsed2, 2)}
        if resp2 is None:
            test_result["error"] = err2
            print(f"  REQUEST FAILED after {test_result['elapsed_seconds']}s: {err2}")
            endpoint_test_results.append(test_result)
            print()
            continue

        ct = resp2.headers.get("Content-Type", "unknown")
        is_pdf = resp2.content[:5] == b"%PDF-"
        is_json = "json" in ct.lower()
        test_result.update({
            "http_status": resp2.status_code, "content_type": ct,
            "response_size_bytes": len(resp2.content), "final_url": resp2.url,
            "redirect_count": len(resp2.history), "is_pdf": is_pdf, "is_json": is_json,
        })
        print(f"  HTTP status   : {resp2.status_code}")
        print(f"  Content-Type  : {ct}")
        print(f"  Response size : {test_result['response_size_bytes']} bytes")
        print(f"  Elapsed       : {test_result['elapsed_seconds']}s")
        print(f"  Final URL     : {resp2.url}")
        print(f"  Redirect count: {test_result['redirect_count']}")
        print(f"  Is PDF?       : {is_pdf}")
        print(f"  Is JSON?      : {is_json}")

        if is_pdf:
            print("  NOT downloading/saving this PDF — diagnostic only.")
        elif is_json:
            try:
                data = resp2.json()
            except Exception as e:
                data = None
                print(f"  JSON parse failed: {e}")
            if isinstance(data, dict):
                doc_url = next((data[k] for k in JSON_DOC_FIELD_KEYS if isinstance(data.get(k), str)), None)
                test_result["document_url_in_json"] = doc_url
                test_result["year_in_json"] = data.get("year")
                if doc_url:
                    print(f"  Document URL found in JSON: {doc_url} (year field: {data.get('year')})")
                else:
                    print("  No documentUrl/fileUrl/pdfUrl/reportUrl field found in JSON.")
            print(f"  Body snippet (500 chars): {resp2.text[:500]!r}")
        elif "html" in ct.lower():
            print(f"  Body snippet (500 chars): {resp2.text[:500]!r}")
        endpoint_test_results.append(test_result)
        print()
    result["endpoint_test_results"] = endpoint_test_results

    print("=" * 78)
    print("CONCLUSION")
    print("=" * 78)
    reliable = next((tr for tr in endpoint_test_results
                      if tr.get("is_pdf") or tr.get("document_url_in_json")), None)
    if reliable:
        found_url = reliable.get("document_url_in_json") or reliable["url"]
        print(f"A reliable path to a real document was found: {found_url}")
    else:
        print(f"No reliable/verified path to the {company_slug} FY{target_fiscal_year} "
              "report was confirmed by this diagnostic.")
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
    parser.add_argument(
        "--diagnose-http", nargs=2, metavar=("COMPANY_SLUG", "FISCAL_YEAR"),
        help="Investigate WHY a registered URL isn't returning a real PDF: "
             "default headers vs. browser-like headers, redirect history, "
             "and (if known) the official report-index page. Read-only, no "
             "retries, no curl, does not save the HTML body to disk, and "
             "does not modify any registry.",
    )
    parser.add_argument(
        "--diagnose-page-links", nargs=2, metavar=("COMPANY_SLUG", "FISCAL_YEAR"),
        help="Fetch a company's known official report-index page EXACTLY "
             "ONCE and inspect its HTML for how the target fiscal year's "
             "report is actually linked (href/iframe/embed/object/JS "
             "reference/data-attribute). Diagnostic only: no PDF download, "
             "no HTML saved to disk, no registry changes.",
    )
    parser.add_argument(
        "--diagnose-report-endpoints", nargs=2, metavar=("COMPANY_SLUG", "FISCAL_YEAR"),
        help="Deeper report-index page investigation: inline <script> "
             "content, external <script src> files (fetched read-only when "
             "report-related, capped at 2), fetch()/axios()/XHR calls, and "
             "JSON documentUrl/fileUrl-style blobs. Tests only HIGH-"
             "CONFIDENCE endpoints found, max 3 GETs. No PDF download, no "
             "HTML/JS saved to disk, no registry/acquisition-logic changes.",
    )
    parser.add_argument(
        "--report-page-url", metavar="URL",
        help="Override the page fetched by --diagnose-report-endpoints for "
             "this run only (e.g. a different official archive/publications "
             "page for the same company). Does not modify "
             "DIAGNOSTIC_REPORT_PAGE_CANDIDATES or any registry — the "
             "override applies only to this single invocation. Ignored by "
             "every other flag.",
    )
    args = parser.parse_args()

    if args.diagnose_http:
        slug, fy = args.diagnose_http
        fy = int(fy)
        if slug not in COMPANY_SOURCE_REGISTRIES:
            print(f"Unknown company_slug: {slug!r}. Known: {sorted(COMPANY_SOURCE_REGISTRIES)}")
            sys.exit(1)
        diagnose_http_investigation(slug, fy)
        return

    if args.diagnose_page_links:
        slug, fy = args.diagnose_page_links
        fy = int(fy)
        if slug not in COMPANY_SOURCE_REGISTRIES:
            print(f"Unknown company_slug: {slug!r}. Known: {sorted(COMPANY_SOURCE_REGISTRIES)}")
            sys.exit(1)
        diagnose_report_page_links(slug, fy)
        return

    if args.diagnose_report_endpoints:
        slug, fy = args.diagnose_report_endpoints
        fy = int(fy)
        if slug not in COMPANY_SOURCE_REGISTRIES:
            print(f"Unknown company_slug: {slug!r}. Known: {sorted(COMPANY_SOURCE_REGISTRIES)}")
            sys.exit(1)
        diagnose_report_endpoints(slug, fy, report_page_url=args.report_page_url)
        return

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
