"""
ingestion/extract_dry_run.py

READ-ONLY, in-memory, no-persistence extraction dry-run.

Takes PDF bytes already in memory (e.g. from requests.get(url).content) and
produces a list of candidate financial facts for human review. This module:

  - operates on bytes only — NEVER accepts or requires a filesystem path,
    NEVER writes a file (no data/raw/, no temp file).
  - imports NO database/Neon code whatsoever (no psycopg, no
    NEON_CONNECTION_STRING, no app.py, no us_xbrl_api.py) — this is a
    structural guarantee, not just a convention: this module cannot reach
    a database even if it tried, because nothing in it imports the means
    to.
  - reuses parser.py's existing CONCEPTS / line_matches() /
    looks_like_financial_value() / parse_number() / NUM_TOKEN_RE rather
    than reimplementing the matching logic.
  - never guesses which numeric column corresponds to the requested
    fiscal year: when a line/window has more than one plausible numeric
    value, BOTH are preserved (value_col1/value_col2) and an explicit
    warning is attached instead of picking one.
  - flags (never silently discards) any concept key not present in the
    documented, live concept_dictionary set (see KNOWN_CONCEPT_KEYS below).
  - surfaces (never silently deduplicates/overwrites) duplicate concept
    matches within one document — each occurrence is kept as its own
    candidate, with a warning attached to every candidate sharing that
    concept key.

This is a dry-run reporting tool only. It does NOT call
register_source_document()/load_facts() (still unimplemented TODOs in
ingestion/load_historical.py) and is not wired to acquire_one_report()/
acquire_reports() — this module performs no acquisition itself; the
caller is responsible for obtaining pdf_bytes (see scripts/dry_run_extract.py).
"""
from __future__ import annotations

import hashlib
import io
from collections import Counter

import pdfplumber

from parser import CONCEPTS, NUM_TOKEN_RE, line_matches, looks_like_financial_value, parse_number

# The 19 concept_dictionary keys confirmed LIVE in Neon as of Phase 1
# preparation (documented in schema.sql's "KNOWN LIVE-SCHEMA DRIFT"
# section): the 11 keys seeded by schema.sql's own INSERT statement
# (which are also exactly parser.py's CONCEPTS keys), plus 8 more
# confirmed present in the live table via a direct query but added
# outside this repo's committed history. Hardcoded here — NOT queried —
# so this dry-run module needs zero database credentials/connection to
# run at all.
KNOWN_CONCEPT_KEYS = frozenset({
    # seeded in schema.sql / also parser.py's own CONCEPTS keys
    "revenue", "gross_profit", "operating_income", "net_income", "eps_basic",
    "total_assets", "total_equity", "total_liabilities",
    "cash_and_equivalents", "cfo", "capex",
    # confirmed live in Neon, not in schema.sql's seed INSERT
    "cost_of_revenue", "eps_basic_continuing", "eps_basic_total",
    "equity_attributable_to_parent", "net_income_attributable_to_parent",
    "net_income_continuing", "net_income_continuing_attributable_to_parent",
    "net_income_total",
})

PDF_MAGIC_BYTES = b"%PDF-"


class PdfBytesInvalid(ValueError):
    """Raised when supplied bytes do not look like a real PDF. Pure
    in-memory check — no I/O, no filesystem access."""


def verify_pdf_bytes(pdf_bytes: bytes) -> None:
    """Raises PdfBytesInvalid if pdf_bytes is empty or does not start with
    the PDF magic bytes (e.g. an HTML error/block page fetched instead of
    a real PDF). Never writes anything, never reads a file."""
    if not pdf_bytes:
        raise PdfBytesInvalid("empty response body — not a PDF")
    if pdf_bytes[:5] != PDF_MAGIC_BYTES:
        raise PdfBytesInvalid(
            "response body does not start with the PDF magic bytes (%PDF-) "
            "— likely an HTML error/block page, not a real PDF"
        )


def extract_from_bytes(pdf_bytes: bytes, ticker: str, fiscal_year: int) -> dict:
    """READ-ONLY, in-memory extraction. Never writes a file (uses
    io.BytesIO, not a temp file), never imports any database code, never
    persists pdf_bytes anywhere beyond this function's own local scope.

    Returns:
      {
        "ticker": ..., "fiscal_year": ..., "document_sha256": ...,
        "page_count": ..., "candidates": [ {...}, ... ],
      }

    Each candidate dict:
      ticker, fiscal_year, statement_type, concept, concept_known,
      reported_label, value_col1, value_col2, currency, unit, source_page,
      extraction_method, confidence, raw_text, warnings (list[str]).
    """
    verify_pdf_bytes(pdf_bytes)
    document_sha256 = hashlib.sha256(pdf_bytes).hexdigest()

    candidates: list[dict] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page_count = len(pdf.pages)
        print(f"[extract_from_bytes] opened PDF: {page_count} pages — starting page-by-page extraction...", flush=True)
        for i, page in enumerate(pdf.pages):
            if i == 0 or (i + 1) % 10 == 0 or (i + 1) == page_count:
                print(f"[extract_from_bytes] page {i + 1}/{page_count}...", flush=True)
            try:
                text = page.extract_text() or ""
                if not text:
                    continue
                lines = text.split("\n")
                # Same 2-line sliding window as parser.py's extract_company(),
                # reused verbatim so a wrapped label still matches its numbers.
                windows2 = [
                    lines[j] + " " + lines[j + 1] if j + 1 < len(lines) else lines[j]
                    for j in range(len(lines))
                ]

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

                        warnings: list[str] = []
                        value_col1 = parsed_vals[0] if len(parsed_vals) > 0 else None
                        value_col2 = parsed_vals[1] if len(parsed_vals) > 1 else None
                        if value_col1 is not None and value_col2 is not None:
                            # NEVER guess which column is the requested fiscal
                            # year — both are preserved as-is, flagged instead.
                            warnings.append(
                                "multiple numeric columns; fiscal year column not disambiguated"
                            )
                        if len(parsed_vals) > 2:
                            warnings.append(
                                f"{len(parsed_vals)} numeric tokens found on this line/window; "
                                "only the first two are captured as value_col1/value_col2 — "
                                "additional values are not represented"
                            )

                        concept_known = concept in KNOWN_CONCEPT_KEYS
                        if not concept_known:
                            warnings.append(
                                f"concept {concept!r} is not in the known/documented "
                                "concept_dictionary set — kept, not discarded"
                            )

                        confidence = "MEDIUM" if len(parsed_vals) <= 3 else "LOW"

                        candidates.append({
                            "ticker": ticker,
                            "fiscal_year": fiscal_year,
                            "statement_type": stmt_type,
                            "concept": concept,
                            "concept_known": concept_known,
                            "reported_label": window.strip()[:90],
                            "value_col1": value_col1,
                            "value_col2": value_col2,
                            "currency": "SAR",
                            "unit": "thousand",
                            "source_page": i + 1,
                            "extraction_method": "pdfplumber_text_keyword_v2_dry_run",
                            "confidence": confidence,
                            "raw_text": window.strip()[:150],
                            "warnings": warnings,
                        })
                del text, lines, windows2
            finally:
                # Bound memory to ~1 page at a time. pdfplumber.PDF.pages
                # (see pdfplumber/pdf.py) keeps every Page wrapper alive for
                # the life of this `with` block, and page.extract_text()
                # lazily populates that Page's cached parsed content (chars,
                # rects, lines, curves, images, layout — see
                # pdfplumber/container.py Container.cached_properties and
                # Page.cached_properties) which is never cleared
                # automatically. Without this call, those caches accumulate
                # across every page processed so far instead of being
                # released once we're done with each page. page.close() is
                # pdfplumber's own public cleanup method for exactly this
                # (it calls flush_cache() and clears the get_textmap
                # lru_cache) — not a workaround, the intended API for
                # bounding memory across a page loop.
                page.close()

    # Duplicate detection: surfaced via a warning on every affected
    # candidate — never merged, never overwritten, every occurrence kept.
    concept_counts = Counter(c["concept"] for c in candidates)
    for c in candidates:
        n = concept_counts[c["concept"]]
        if n > 1:
            c["warnings"].append(
                f"concept {c['concept']!r} detected {n} times across this document "
                "— not deduplicated, review each candidate"
            )

    return {
        "ticker": ticker,
        "fiscal_year": fiscal_year,
        "document_sha256": document_sha256,
        "page_count": page_count,
        "candidates": candidates,
    }
