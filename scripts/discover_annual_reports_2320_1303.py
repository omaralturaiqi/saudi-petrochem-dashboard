#!/usr/bin/env python3
"""
scripts/discover_annual_reports_2320_1303.py

Read-only DISCOVERY ONLY for two companies flagged by a portfolio-wide
Abnormal Return analysis (253 companies, 2025): ALBABTAIN (ticker 2320,
Capital Goods, +82.2% abnormal return) and EIC (ticker 1303, Capital
Goods, +67.6% abnormal return). The question this discovery step exists
to support: is FY2024/FY2025 financial data available to check whether
that price move is a Confirmed Recovery or a Momentum Risk — neither of
which this script itself answers; it only finds candidate annual-report
document links.

This script does NOT download or store any PDF. It discovers candidate
links and verifies their EXISTENCE (HTTP HEAD, or a tiny streamed GET
capped at a few bytes if HEAD is rejected) — never a full GET, never a
byte written to disk. This matches this project's established principle
(see ingestion/load_historical.py's acquisition layer): extract facts,
don't hoard files.

STEP 1 FINDING (verified this session, read-only, before writing this
file): neither ticker 2320 nor 1303 appears anywhere in
ingestion/load_historical.py's COMPANY_SOURCE_REGISTRIES or
CONFIRMED_COMPANY_TICKERS (grepped directly — zero matches for "2320" or
"1303" in that file). That registry's entire target universe (10
companies: sabic, yansab, advanced_petrochemical, saudi_kayan, sipchem,
tasnee, siig, nama_chemicals, alujain, sabic_agri_nutrients) is
Materials-sector petrochemical companies per README.md's documented
"PLANNED" roadmap — ALBABTAIN and EIC are Capital Goods, outside that
documented universe entirely. This script is therefore new discovery
work, not an extension of an existing per-company registry entry, and
deliberately does NOT add anything to COMPANY_SOURCE_REGISTRIES itself
(that remains a human-curated, individually-verified registry — see its
own module docstring in ingestion/load_historical.py).

CANDIDATE STARTING PAGES — read this before trusting anything below:
  This script needs one real starting URL per company to crawl (the same
  role DIAGNOSTIC_REPORT_PAGE_CANDIDATES plays for SABIC in
  scripts/acquire_official_pdfs.py, whose own comment says its SABIC entry
  was "discovered via WebSearch" — this script follows that exact same,
  already-established precedent, not a new one). CANDIDATE_IR_PAGES below
  was populated via a real WebSearch this session (2026-09-02):
    - albabtain (2320): https://www.al-babtain.com.sa/investor-relations/
      — the company's own investor-relations page, per WebSearch results
      naming it directly as Al-Babtain's official IR page.
    - eic (1303): https://eic.com.sa/eic-ir?lang=en
      — the company's own investor-relations page. A real EIC PDF URL
      also appeared directly in WebSearch results (a 2022 Board of
      Directors report at eic.com.sa/uploads/investor_downloads/...) —
      that specific URL is NOT hardcoded as a target here (it's a 2022
      document, not FY2024/2025, and using it directly would be exactly
      the kind of unverified assumption this project avoids), but its
      URL PATH PATTERN (/uploads/investor_downloads/) is added to
      DOCUMENT_URL_MARKERS below as a second real, observed EIC document
      path alongside SABIC's already-established /images/ pattern.
  Neither candidate URL, nor any link this script discovers by crawling
  them, was independently fetched or verified by Claude in this session
  — WebFetch was confirmed FULLY BLOCKED in this sandbox earlier this
  session (a neutral control-domain test against example.com was also
  blocked, ruling out a domain-specific block). This script's actual GET/
  HEAD requests only happen when it is run somewhere with real network
  access (Render Shell, per this task's own instructions) — nothing in
  this file has been executed against the live internet by Claude.

WHAT THIS SCRIPT DOES (when actually run, e.g. via Render Shell):
  1. For each company in TARGET_COMPANIES, GETs its candidate_ir_url
     (ONE request, read-only, browser-like headers — same header set
     scripts/acquire_official_pdfs.py already uses for this exact
     reason: some Saudi corporate sites reject requests with no
     User-Agent).
  2. Parses the returned HTML (a small custom parser, not imported from
     acquire_official_pdfs.py — this script is self-contained, matching
     scripts/fetch_market_index.py's own precedent of hand-duplicating a
     pattern rather than importing and coupling to an existing module)
     for <a href>, <iframe src>, and <embed src> targets, resolved to
     absolute URLs.
  3. Classifies each candidate HIGH_CONFIDENCE / POSSIBLE / UNRELATED
     (see classify_candidate()) — HIGH_CONFIDENCE requires BOTH a
     real-document-shaped URL (see DOCUMENT_URL_MARKERS) AND a
     TARGET_FISCAL_YEARS marker (2024 or 2025) somewhere in the URL or
     its link text; POSSIBLE is a document-shaped URL with no year match,
     or an "annual report"-labeled link with a year match but no document
     extension/path yet; everything else is UNRELATED. This mirrors
     scripts/acquire_official_pdfs.py's _classify_endpoint_candidate()
     logic (same three-tier shape, same "real document URL AND year" bar
     for HIGH_CONFIDENCE), reimplemented here rather than imported to
     keep this script single-file and self-contained.
  4. For up to MAX_ENDPOINTS_TO_VERIFY HIGH_CONFIDENCE candidates per
     company, verifies existence only: HTTP HEAD first; if the server
     rejects HEAD (405) or returns no useful Content-Type, falls back to
     a GET with the connection closed after reading a handful of bytes
     (just enough to check the '%PDF-' magic number) — never reads or
     buffers a full response body, never writes anything to disk.
  5. Prints a per-company summary: HIGH-CONFIDENCE / POSSIBLE / UNRELATED
     candidate counts and URLs, and the verification result for each
     HIGH-CONFIDENCE candidate actually tested.

WHAT THIS SCRIPT DELIBERATELY DOES NOT DO:
  - Never downloads or saves a PDF, or any other file, to disk.
  - Never writes to COMPANY_SOURCE_REGISTRIES, CONFIRMED_COMPANY_TICKERS,
    or any other file in this repo — pure stdout discovery only.
  - Never connects to Neon — no financial-data ingestion happens here or
    is implied by this script; that remains a distinct, later, explicit
    task even if a HIGH-CONFIDENCE PDF is found.
  - Never asserts a discovered candidate URL IS the correct FY2024/2025
    annual report — HIGH_CONFIDENCE means "matches this project's own
    document-URL and target-year heuristics", not "confirmed correct".
    A human (or a later, explicit task) still reviews the actual PDF.
  - Never retries a failed request or expands past
    MAX_ENDPOINTS_TO_VERIFY — no retry storms against these companies'
    sites, same discipline as scripts/acquire_official_pdfs.py.

USAGE:
    python3 scripts/discover_annual_reports_2320_1303.py
        # no NEON_CONNECTION_STRING needed — this script never touches
        # Neon. Needs real network access to al-babtain.com.sa and
        # eic.com.sa, which this authoring sandbox does not have (see
        # "CANDIDATE STARTING PAGES" above) — intended to be run via
        # Render Shell, per this task's own instructions.
"""
from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

TARGET_COMPANIES: dict[str, dict[str, str]] = {
    "albabtain": {
        "ticker": "2320",
        "name_en": "Al-Babtain Power and Telecommunication Co.",
        "candidate_ir_url": "https://www.al-babtain.com.sa/investor-relations/",
    },
    "eic": {
        "ticker": "1303",
        "name_en": "Electrical Industries Company (EIC)",
        "candidate_ir_url": "https://eic.com.sa/eic-ir?lang=en",
    },
}

TARGET_FISCAL_YEARS = (2024, 2025)
MAX_ENDPOINTS_TO_VERIFY = 3  # per company — same fixed cap discipline as
                              # scripts/acquire_official_pdfs.py's own
                              # diagnose_report_endpoints() (max 3 GETs).

# Path markers this codebase has actually observed on a real, official
# document URL: ".pdf" (universal), "/images/" (SABIC's own CDN path, per
# scripts/acquire_official_pdfs.py's _looks_like_document_url()), and
# "/uploads/investor_downloads/" (EIC's own path — a real EIC PDF URL at
# this exact path appeared directly in this session's WebSearch results,
# see module docstring). Not a guess/pattern-generated list — each entry
# has a real, observed source.
DOCUMENT_URL_MARKERS = (".pdf", "/images/", "/uploads/investor_downloads/")

ANNUAL_REPORT_KEYWORDS = (
    "annual report", "annual-report", "board of directors report",
    "board report", "التقرير السنوي",
)

# Same browser-like headers scripts/acquire_official_pdfs.py already uses
# for this exact reason: some Saudi corporate sites reject requests with
# no User-Agent.
BROWSER_LIKE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class _LinkExtractor(HTMLParser):
    """Minimal, self-contained HTML link extractor: collects every href
    (a, link) and src (iframe, embed, object data) attribute value, along
    with a short surrounding-text context for classification. Not
    imported from scripts/acquire_official_pdfs.py's own
    _ReportLinkHTMLParser — this script is deliberately self-contained
    (see module docstring)."""

    LINK_ATTRS = {
        "a": "href", "link": "href",
        "iframe": "src", "embed": "src", "object": "data",
    }

    def __init__(self) -> None:
        super().__init__()
        self.candidates: list[dict] = []
        self._current_text_parts: list[str] = []
        self._pending: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_name = self.LINK_ATTRS.get(tag)
        if not attr_name:
            return
        attrs_dict = dict(attrs)
        value = attrs_dict.get(attr_name)
        if not value:
            return
        self._pending = {"raw_value": value, "tag": tag, "context": ""}
        self._current_text_parts = []

    def handle_data(self, data: str) -> None:
        if self._pending is not None:
            self._current_text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._pending is not None and tag in self.LINK_ATTRS:
            self._pending["context"] = " ".join(self._current_text_parts).strip()
            self.candidates.append(self._pending)
            self._pending = None
            self._current_text_parts = []


def extract_candidate_links(html_text: str, base_url: str) -> list[dict]:
    """Pure given html_text/base_url — offline-testable without network."""
    parser = _LinkExtractor()
    try:
        parser.feed(html_text)
    except Exception:
        pass  # malformed HTML — keep whatever was parsed before the failure
    results = []
    for c in parser.candidates:
        resolved = urljoin(base_url, c["raw_value"])
        results.append({
            "raw_value": c["raw_value"],
            "resolved_url": resolved,
            "context": c["context"],
            "tag": c["tag"],
        })
    return results


def _looks_like_document_url(url: str) -> bool:
    """Pure, offline-testable. See DOCUMENT_URL_MARKERS for why each
    marker is trusted."""
    path = urlparse(url).path.lower()
    return path.endswith(".pdf") or any(marker in path for marker in DOCUMENT_URL_MARKERS if marker != ".pdf")


def classify_candidate(url: str, context: str, target_years: tuple[int, ...] = TARGET_FISCAL_YEARS) -> str:
    """Pure, offline-testable. HIGH_CONFIDENCE / POSSIBLE / UNRELATED —
    see module docstring point 3 for the exact rule. Mirrors
    scripts/acquire_official_pdfs.py's _classify_endpoint_candidate()
    shape (document-URL-ness AND year match required for HIGH_CONFIDENCE),
    reimplemented here rather than imported."""
    is_document = _looks_like_document_url(url)
    haystack = f"{url} {context or ''}".lower()
    has_year = any(str(y) in haystack for y in target_years)
    has_report_keyword = any(kw in haystack for kw in ANNUAL_REPORT_KEYWORDS)

    if is_document and has_year:
        return "HIGH_CONFIDENCE"
    if is_document or (has_report_keyword and has_year):
        return "POSSIBLE"
    return "UNRELATED"


def fetch_ir_page(url: str) -> dict:
    """ONE GET request, read-only, browser-like headers. Never used to
    download a PDF — that path is handled separately by
    verify_endpoint_exists(), which never buffers a full body either."""
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
    """Read-only, no save: HTTP HEAD first. If the server rejects HEAD
    (405) or returns no useful Content-Type, falls back to a GET with the
    connection closed after reading a handful of bytes (just enough to
    check the '%PDF-' magic number) — never reads or buffers a full
    response body, never writes anything to disk. This is the "HTTP HEAD
    أو GET خفيف" this task explicitly asked for, nothing beyond it."""
    import requests

    result: dict = {"url": url}
    try:
        resp = requests.head(url, headers=BROWSER_LIKE_HEADERS, timeout=20, allow_redirects=True)
        result.update({
            "method": "HEAD",
            "http_status": resp.status_code,
            "content_type": resp.headers.get("Content-Type"),
            "content_length": resp.headers.get("Content-Length"),
        })
    except Exception as e:
        result["head_error"] = f"{type(e).__name__}: {e}"
        result["http_status"] = None

    needs_fallback = result.get("http_status") in (None, 405) or not result.get("content_type")
    if needs_fallback:
        try:
            resp2 = requests.get(url, headers=BROWSER_LIKE_HEADERS, timeout=20, stream=True)
            chunk = next(resp2.iter_content(chunk_size=8), b"")
            resp2.close()  # never read the rest of the body — connection closed immediately
            result.update({
                "method": "GET (streamed, <=8 bytes read then closed — not saved)",
                "http_status": resp2.status_code,
                "content_type": resp2.headers.get("Content-Type"),
                "is_pdf_magic_bytes": chunk[:5] == b"%PDF-",
            })
        except Exception as e:
            result["fallback_get_error"] = f"{type(e).__name__}: {e}"

    return result


def discover_for_company(slug: str, info: dict) -> dict:
    ticker = info["ticker"]
    name_en = info["name_en"]
    ir_url = info["candidate_ir_url"]

    print("=" * 78)
    print(f"DISCOVER ANNUAL REPORT: {name_en} (ticker={ticker}, slug={slug})")
    print(f"Candidate IR page (see module docstring — WebSearch-sourced, not fetched by Claude): {ir_url}")
    print("=" * 78)

    page = fetch_ir_page(ir_url)
    if page.get("error"):
        print(f"REQUEST FAILED: {page['error']}")
        print("=" * 78)
        return {"slug": slug, "ticker": ticker, "page": page, "candidates": [], "verified": []}

    print(f"HTTP status: {page['http_status']}  Content-Type: {page['content_type']}  final URL: {page['final_url']}")
    if page["http_status"] != 200 or not page["html_text"]:
        print("Response is not a 200 HTML page — nothing to parse.")
        print("=" * 78)
        return {"slug": slug, "ticker": ticker, "page": page, "candidates": [], "verified": []}

    raw_candidates = extract_candidate_links(page["html_text"], page["final_url"])
    candidates = []
    for c in raw_candidates:
        c["classification"] = classify_candidate(c["resolved_url"], c["context"])
        candidates.append(c)

    for label, key in (
        ("HIGH-CONFIDENCE", "HIGH_CONFIDENCE"),
        ("POSSIBLE", "POSSIBLE"),
        ("UNRELATED", "UNRELATED"),
    ):
        matches = [c for c in candidates if c["classification"] == key]
        print(f"\n{label} ({len(matches)}):")
        if not matches:
            print("  (none found)")
        for c in matches[:20] if key == "UNRELATED" else matches:
            print(f"  [{c['tag']}] {c['resolved_url']}  — context: {c['context'][:80]!r}")
        if key == "UNRELATED" and len(matches) > 20:
            print(f"  ... and {len(matches) - 20} more UNRELATED links not printed")

    seen_urls: set[str] = set()
    high_confidence = []
    for c in candidates:
        if c["classification"] == "HIGH_CONFIDENCE" and c["resolved_url"] not in seen_urls:
            high_confidence.append(c)
            seen_urls.add(c["resolved_url"])
    to_verify = high_confidence[:MAX_ENDPOINTS_TO_VERIFY]
    if len(high_confidence) > MAX_ENDPOINTS_TO_VERIFY:
        skipped = [c["resolved_url"] for c in high_confidence[MAX_ENDPOINTS_TO_VERIFY:]]
        print(f"\nNOTE: {len(high_confidence)} HIGH-CONFIDENCE candidates found; verifying only the "
              f"first {MAX_ENDPOINTS_TO_VERIFY}. Not verified: {skipped}")

    print(f"\nVERIFYING {len(to_verify)} HIGH-CONFIDENCE CANDIDATE(S) (existence only — no download, no save)")
    verified = []
    for c in to_verify:
        v = verify_endpoint_exists(c["resolved_url"])
        verified.append(v)
        print(f"  {c['resolved_url']}")
        for k, val in v.items():
            if k != "url":
                print(f"    {k}: {val}")

    print("=" * 78)
    return {"slug": slug, "ticker": ticker, "page": page, "candidates": candidates, "verified": verified}


def main() -> None:
    print("#" * 78)
    print("DISCOVER ANNUAL REPORTS — ALBABTAIN (2320) and EIC (1303)")
    print("Read-only discovery only. No PDF is downloaded or stored by this script.")
    print("No Neon connection of any kind — pure stdout discovery.")
    print("#" * 78)
    print()

    results = {}
    for slug, info in TARGET_COMPANIES.items():
        results[slug] = discover_for_company(slug, info)
        print()

    print("#" * 78)
    print("SUMMARY")
    for slug, r in results.items():
        n_high = len([c for c in r["candidates"] if c["classification"] == "HIGH_CONFIDENCE"])
        n_possible = len([c for c in r["candidates"] if c["classification"] == "POSSIBLE"])
        print(f"  {slug} (ticker={r['ticker']}): {n_high} HIGH-CONFIDENCE, {n_possible} POSSIBLE candidates found")
    print("PDFs downloaded/stored by this script: 0")
    print("Neon writes issued by this script: 0")
    print("#" * 78)


if __name__ == "__main__":
    main()
