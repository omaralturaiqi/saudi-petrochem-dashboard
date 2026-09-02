#!/usr/bin/env python3
"""
scripts/discover_company_annual_reports.py

Generalizes the discovery pattern proven for SABIC
(scripts/acquire_official_pdfs.py's diagnose_report_endpoints()) and for
ALBABTAIN/EIC (scripts/discover_annual_reports_2320_1303.py, hand-coded
per company) into ONE script that works against ANY company's investor-
relations page, given just {ticker: ir_page_url}. Live network tests this
session (real Render Shell run, reported by the project owner) confirmed
individual company sites (almarai.com, alrajhibank.com.sa, aramco.com,
stc.com.sa) ARE reachable — unlike saudiexchange.sa/argaam.com, which are
blocked — so a per-company-site discovery script is the viable path
forward, at the scale of however many of the 253 Tadawul companies the
project owner supplies IR page URLs for.

This script does NOT modify scripts/acquire_official_pdfs.py, and does
NOT import from it or from scripts/discover_annual_reports_2320_1303.py —
self-contained, matching this session's established precedent
(scripts/fetch_market_index.py duplicating scripts/fetch_market_prices.py's
pattern by hand rather than importing/coupling to it).

INPUT — read this before running: COMPANY_IR_PAGES below is intentionally
EMPTY. The project owner is discovering the real ~50 (ticker, ir_page_url)
pairs themselves (real search, not invented by this script or by Claude)
and will supply them in a later step. This file only builds the structure
and extraction/classification logic — filling COMPANY_IR_PAGES with
guessed URLs would be exactly the kind of fabrication this project
avoids throughout its history (see e.g. schema_us_xbrl.sql's and
discover_tadawul_companies.py's own provenance-honesty headers). Running
this script with COMPANY_IR_PAGES empty is safe and does nothing (see
main()) — that is deliberate, not a bug.

WHAT THIS SCRIPT DOES (when actually run with real entries, e.g. via
Render Shell):
  1. For each (ticker, ir_page_url) in COMPANY_IR_PAGES: ONE GET request,
     read-only, browser-like headers (same header set already used by
     scripts/acquire_official_pdfs.py and
     scripts/discover_annual_reports_2320_1303.py, for the same reason:
     some Saudi corporate sites reject requests with no User-Agent).
  2. Scans the raw HTML for candidate report links — an <a href="...">
     whose URL ends in ".pdf" or contains "annual-report"/"annual_report"
     (case-insensitive), or whose link text contains those same markers.
     For each candidate link, associates it with the nearest 20XX-shaped
     year, in this priority order (see extract_year_report_pairs()):
       a. a year embedded directly in the link's own href or link text
          (e.g. ".../annual-report-2024.pdf", or link text "Annual
          Report 2024") — unambiguous, always wins if present.
       b. otherwise, the CLOSEST 20XX year label appearing in the raw
          HTML text within WINDOW_CHARS characters immediately BEFORE
          the link (the common "<year label> ... <Download PDF link>"
          site pattern) — "closest" specifically to avoid a year label
          from a DIFFERENT, nearby report-list item being mismatched to
          this link (the "no cross-contamination between years"
          requirement).
       c. otherwise, the closest 20XX year label within a smaller
          look-ahead window AFTER the link, as a last resort for the
          less common "<Download PDF link> ... <year label>" ordering.
     A link with no year found in any of the three ways is NOT
     force-matched to some other year — it is reported separately as
     unmatched, never silently guessed.
  3. Splits matched (year, url) pairs into two explicit buckets:
       - years >= TARGET_YEAR_MIN (2015): kept as "extracted" results.
       - years < TARGET_YEAR_MIN: NOT discarded — reported in a
         separate excluded_pre_2015 list (ticker, year, url) so a
         pre-2015 report is still visible in the output, just not
         treated as a target-range result. Nothing before 2015 is
         silently dropped.
  4. Never downloads or saves a PDF. Up to MAX_ENDPOINTS_TO_VERIFY
     extracted (>=2015) links per company are existence-checked only
     (HTTP HEAD, falling back to a GET closed after a few bytes if HEAD
     is rejected) — same no-download discipline as every other
     discovery script in this project's history.

WHAT THIS SCRIPT DELIBERATELY DOES NOT DO:
  - Never invents an IR page URL — COMPANY_IR_PAGES starts empty and
    stays empty until the project owner supplies real, actually-
    discovered URLs.
  - Never modifies scripts/acquire_official_pdfs.py or any
    *_SOURCE_REGISTRY / CONFIRMED_COMPANY_TICKERS in
    ingestion/load_historical.py.
  - Never downloads or stores a PDF, or any other file, to disk.
  - Never connects to Neon — pure stdout discovery only.
  - Never force-matches a report link to a year it could not actually
    find nearby — an unmatched link is reported as unmatched, not
    guessed.

USAGE:
    python3 scripts/discover_company_annual_reports.py
        # Safe to run right now: COMPANY_IR_PAGES is empty, so this does
        # nothing but print that fact. Once real entries are added, it
        # needs real network access to each company's own site — this
        # authoring sandbox does not have that (WebFetch confirmed fully
        # blocked here this session); intended to run via Render Shell.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# ----------------------------------------------------------------------------
# INPUT — see module docstring's "INPUT" section. 5 real, project-owner-
# verified (ticker, ir_page_url) pairs for a live test batch — not
# discovered or verified by Claude; supplied directly for this run.
# ----------------------------------------------------------------------------
COMPANY_IR_PAGES: dict[str, str] = {
    "2222": "https://www.aramco.com/en/investors/annual-report",
    "2280": "https://www.almarai.com/en/corporate/investor-relations/financial-information",
    "6004": "https://www.catrion.com/investor-relation",
    "1120": "https://www.alrajhibank.com.sa/en/About-alrajhi-bank/Investor-Relations",
    "7010": "https://www.stc.com/content/stcgroupwebsite/sa/en/investors/financial-reports/annual-reports.html",
}

TARGET_YEAR_MIN = 2015
YEAR_SEARCH_MIN = 2000   # lower sanity bound for what counts as a "year" at
YEAR_SEARCH_MAX = 2035   # all, so an unrelated 4-digit number isn't misread
                          # as a report year.
WINDOW_CHARS_BEFORE = 400  # how far back (raw HTML chars) to look for a
                            # year label preceding a candidate link.
WINDOW_CHARS_AFTER = 150   # smaller look-ahead window for the less common
                            # "link, then year label" ordering.
MAX_ENDPOINTS_TO_VERIFY = 3  # per company — same fixed cap discipline as
                              # every other discovery script this session.

REPORT_URL_MARKERS = (".pdf", "annual-report", "annual_report")

YEAR_PATTERN = re.compile(r"\b(20\d{2})\b")
LINK_PATTERN = re.compile(r'<a\b[^>]*?\bhref\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                           re.IGNORECASE | re.DOTALL)
TAG_STRIP_PATTERN = re.compile(r"<[^>]+>")

BROWSER_LIKE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _looks_like_report_link(url: str, link_text: str) -> bool:
    """Pure, offline-testable. REPORT_URL_MARKERS' "annual-report"/
    "annual_report" entries are checked against the URL as-is (a URL
    genuinely does use - or _, never a space), but against the human-
    readable link text with separators normalized to spaces first — real
    link text overwhelmingly reads "Annual Report 2024", not
    "Annual-Report 2024" — so "annual report" (space-separated) is
    additionally accepted there. ".pdf" is checked against both
    unmodified (a link's visible text is never itself a filename)."""
    haystack = f"{url} {link_text}".lower()
    if any(marker in haystack for marker in REPORT_URL_MARKERS):
        return True
    normalized_text = re.sub(r"[-_]", " ", link_text.lower())
    return "annual report" in normalized_text


def _years_in_range(text: str) -> list[tuple[int, int]]:
    """Returns [(year, match_start_offset), ...] for every YEAR_SEARCH_MIN..
    YEAR_SEARCH_MAX year found in text, in order of appearance."""
    out = []
    for m in YEAR_PATTERN.finditer(text):
        year = int(m.group(1))
        if YEAR_SEARCH_MIN <= year <= YEAR_SEARCH_MAX:
            out.append((year, m.start()))
    return out


def _find_associated_year(html_text: str, link_start: int, link_end: int,
                           href: str, link_text: str,
                           prev_link_end: int = 0, next_link_start: int | None = None) -> int | None:
    """Pure, offline-testable. Implements the 3-tier priority described in
    the module docstring's point 2: year embedded in the link itself,
    then nearest preceding year label, then nearest following year label.
    Returns None if no year is found by any of the three — never guesses
    a fallback.

    prev_link_end / next_link_start bound the search windows to never
    cross into a NEIGHBORING candidate link's own text — without this, a
    report-list item with no year of its own could wrongly pick up the
    year label that actually belongs to the PREVIOUS list item, once that
    item's year happens to fall within WINDOW_CHARS_BEFORE of this link
    (the exact cross-contamination case this function must avoid)."""
    # (a) year embedded directly in the link's own href/text.
    in_link_years = _years_in_range(href) + _years_in_range(link_text)
    if in_link_years:
        return in_link_years[0][0]

    # (b) closest preceding year label within WINDOW_CHARS_BEFORE, but
    # never reaching back past the end of the previous candidate link.
    window_start = max(0, link_start - WINDOW_CHARS_BEFORE, prev_link_end)
    before_text = html_text[window_start:link_start]
    before_years = _years_in_range(before_text)
    if before_years:
        # last occurrence in the window = closest to the link
        return before_years[-1][0]

    # (c) closest following year label within WINDOW_CHARS_AFTER, but
    # never reaching forward past the start of the next candidate link.
    window_end = link_end + WINDOW_CHARS_AFTER
    if next_link_start is not None:
        window_end = min(window_end, next_link_start)
    after_text = html_text[link_end:window_end]
    after_years = _years_in_range(after_text)
    if after_years:
        # first occurrence in the window = closest to the link
        return after_years[0][0]

    return None


def extract_year_report_pairs(html_text: str, base_url: str) -> dict:
    """Pure, offline-testable given raw HTML text. Returns:
      {
        "extracted": [{"year": int, "url": str, "raw_href": str}, ...]   # year >= TARGET_YEAR_MIN
        "excluded_pre_2015": [{"year": int, "url": str, "raw_href": str}, ...]  # year < TARGET_YEAR_MIN, reported not dropped
        "unmatched": [{"url": str, "raw_href": str}, ...]  # report-shaped link, no year found nearby
      }
    Every candidate link ends up in exactly one of the three buckets —
    none are silently discarded."""
    from urllib.parse import urljoin

    extracted: list[dict] = []
    excluded_pre_2015: list[dict] = []
    unmatched: list[dict] = []

    # Collect ALL <a> tags first (not just report-shaped ones) so a
    # neighboring link of ANY kind — not only another report link — bounds
    # the year-search window and prevents it from crossing into a
    # different list item. See _find_associated_year()'s docstring.
    all_links = list(LINK_PATTERN.finditer(html_text))

    for i, m in enumerate(all_links):
        href = m.group(1)
        link_text = TAG_STRIP_PATTERN.sub(" ", m.group(2))
        if not _looks_like_report_link(href, link_text):
            continue

        prev_link_end = all_links[i - 1].end() if i > 0 else 0
        next_link_start = all_links[i + 1].start() if i + 1 < len(all_links) else None

        year = _find_associated_year(
            html_text, m.start(), m.end(), href, link_text,
            prev_link_end=prev_link_end, next_link_start=next_link_start,
        )
        resolved_url = urljoin(base_url, href)
        entry = {"year": year, "url": resolved_url, "raw_href": href}

        if year is None:
            unmatched.append({"url": resolved_url, "raw_href": href})
        elif year >= TARGET_YEAR_MIN:
            extracted.append(entry)
        else:
            excluded_pre_2015.append(entry)

    return {"extracted": extracted, "excluded_pre_2015": excluded_pre_2015, "unmatched": unmatched}


def fetch_ir_page(url: str) -> dict:
    """ONE GET request, read-only, browser-like headers."""
    import requests

    try:
        resp = requests.get(url, headers=BROWSER_LIKE_HEADERS, timeout=30)
    except Exception as e:
        return {"url": url, "error": f"{type(e).__name__}: {e}"}
    return {
        "url": url,
        "http_status": resp.status_code,
        "content_type": resp.headers.get("Content-Type", "unknown"),
        "final_url": resp.url,
        "html_text": resp.text if "html" in resp.headers.get("Content-Type", "").lower() else None,
    }


def verify_endpoint_exists(url: str) -> dict:
    """Read-only, no save: HTTP HEAD first, falling back to a GET closed
    after a handful of bytes if HEAD is rejected — same discipline as
    scripts/discover_annual_reports_2320_1303.py's verify_endpoint_exists()
    (reimplemented here, not imported, per this script's self-contained
    design)."""
    import requests

    result: dict = {"url": url}
    try:
        resp = requests.head(url, headers=BROWSER_LIKE_HEADERS, timeout=20, allow_redirects=True)
        result.update({
            "method": "HEAD",
            "http_status": resp.status_code,
            "content_type": resp.headers.get("Content-Type"),
        })
    except Exception as e:
        result["head_error"] = f"{type(e).__name__}: {e}"
        result["http_status"] = None

    needs_fallback = result.get("http_status") in (None, 405) or not result.get("content_type")
    if needs_fallback:
        try:
            resp2 = requests.get(url, headers=BROWSER_LIKE_HEADERS, timeout=20, stream=True)
            chunk = next(resp2.iter_content(chunk_size=8), b"")
            resp2.close()
            result.update({
                "method": "GET (streamed, <=8 bytes read then closed — not saved)",
                "http_status": resp2.status_code,
                "content_type": resp2.headers.get("Content-Type"),
                "is_pdf_magic_bytes": chunk[:5] == b"%PDF-",
            })
        except Exception as e:
            result["fallback_get_error"] = f"{type(e).__name__}: {e}"

    return result


def discover_for_company(ticker: str, ir_page_url: str) -> dict:
    print("=" * 78)
    print(f"DISCOVER ANNUAL REPORTS: ticker={ticker}")
    print(f"IR page: {ir_page_url}")
    print("=" * 78)

    page = fetch_ir_page(ir_page_url)
    if page.get("error"):
        print(f"REQUEST FAILED: {page['error']}")
        print("=" * 78)
        return {"ticker": ticker, "page": page, "extracted": [], "excluded_pre_2015": [], "unmatched": [], "verified": []}

    print(f"HTTP status: {page['http_status']}  Content-Type: {page['content_type']}  final URL: {page['final_url']}")
    if page["http_status"] != 200 or not page["html_text"]:
        print("Response is not a 200 HTML page — nothing to parse.")
        print("=" * 78)
        return {"ticker": ticker, "page": page, "extracted": [], "excluded_pre_2015": [], "unmatched": [], "verified": []}

    pairs = extract_year_report_pairs(page["html_text"], page["final_url"])

    print(f"\nEXTRACTED (year >= {TARGET_YEAR_MIN}, {len(pairs['extracted'])}):")
    for e in sorted(pairs["extracted"], key=lambda x: -x["year"]):
        print(f"  {e['year']}: {e['url']}")
    print(f"\nEXCLUDED_PRE_{TARGET_YEAR_MIN} (year < {TARGET_YEAR_MIN}, reported not dropped, {len(pairs['excluded_pre_2015'])}):")
    for e in sorted(pairs["excluded_pre_2015"], key=lambda x: -x["year"]):
        print(f"  {e['year']}: {e['url']}")
    print(f"\nUNMATCHED (report-shaped link, no nearby year found, {len(pairs['unmatched'])}):")
    for u in pairs["unmatched"]:
        print(f"  {u['url']}")

    to_verify = pairs["extracted"][:MAX_ENDPOINTS_TO_VERIFY]
    if len(pairs["extracted"]) > MAX_ENDPOINTS_TO_VERIFY:
        print(f"\nNOTE: {len(pairs['extracted'])} extracted links found; verifying only the first "
              f"{MAX_ENDPOINTS_TO_VERIFY}.")

    print(f"\nVERIFYING {len(to_verify)} EXTRACTED LINK(S) (existence only — no download, no save)")
    verified = []
    for e in to_verify:
        v = verify_endpoint_exists(e["url"])
        verified.append(v)
        print(f"  {e['url']}")
        for k, val in v.items():
            if k != "url":
                print(f"    {k}: {val}")

    print("=" * 78)
    return {"ticker": ticker, "page": page, **pairs, "verified": verified}


def main() -> None:
    print("#" * 78)
    print("DISCOVER COMPANY ANNUAL REPORTS — generic, any company IR page")
    print("Read-only discovery only. No PDF is downloaded or stored by this script.")
    print("No Neon connection of any kind — pure stdout discovery.")
    print("#" * 78)
    print()

    if not COMPANY_IR_PAGES:
        print("COMPANY_IR_PAGES is empty — nothing configured yet. This is expected: the "
              "project owner supplies real (ticker, ir_page_url) pairs in a later step. "
              "Nothing to do; exiting cleanly.")
        print("#" * 78)
        return

    results = {}
    for ticker, ir_page_url in COMPANY_IR_PAGES.items():
        results[ticker] = discover_for_company(ticker, ir_page_url)
        print()

    print("#" * 78)
    print("SUMMARY")
    for ticker, r in results.items():
        print(f"  {ticker}: {len(r['extracted'])} extracted, {len(r['excluded_pre_2015'])} pre-{TARGET_YEAR_MIN} "
              f"excluded, {len(r['unmatched'])} unmatched")
    print("PDFs downloaded/stored by this script: 0")
    print("Neon writes issued by this script: 0")
    print("#" * 78)


if __name__ == "__main__":
    main()
