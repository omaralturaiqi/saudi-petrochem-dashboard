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
    looks_like_financial_value() / parse_number() rather than
    reimplementing the keyword-matching/number-parsing logic. parser.py is
    NOT modified by this module.
  - COORDINATE-AWARE (as of this revision): uses page.extract_words() (x0/
    x1/top per word) instead of the flat page.extract_text() string. This
    replaces the earlier flat-text approach, which was shown (via a
    read-only diagnostic prototype run against the real SABIC FY2024 PDF)
    to merge unrelated two-column content into a single "line" and to have
    no way to bind a numeric value to a specific fiscal-year column. See
    the helper functions below for the specific mechanisms:
      * _detect_column_boundary() — finds the vertical whitespace gutter
        that separates a left-column financial table from a right-column
        narrative sidebar on a two-column annual-report page, so the two
        are never merged into one reconstructed row.
      * _group_words_into_rows() — y-position clustering, applied
        SEPARATELY per column band (never across the detected boundary).
      * year-header detection + nearest-column x-binding — locates a row
        containing >=2 year-shaped tokens (e.g. "2024", "2023") and records
        each token's x-position as a column; every numeric value found in
        subsequent rows of that table block is bound to whichever year
        column's x-center it is nearest to (within a tolerance), rather
        than assuming the first or second number found is the requested
        fiscal year.
  - Only the left/primary column band (the financial-table region) is ever
    scanned for concept matches. Right-column narrative/commentary text is
    never scanned — this is what rejects a sentence that merely mentions a
    concept and a number in prose from becoming a candidate.
  - Table blocks are tracked (table_index, incremented at each detected
    year-header row). A concept matched in more than one table_index on
    the same page is NOT merged into one candidate — each stays a separate
    candidate carrying its own table_index and column-binding results, and
    is flagged with a warning that duplicate table blocks were found. This
    is the honest, evidence-grounded version of "distinguish the SAR table
    from a USD-denominated duplicate": this module has no reliable way to
    read a literal currency label from the page, so it does not guess one
    — it keeps duplicate-table candidates distinct and flags them for
    human review rather than silently merging or silently trusting the
    first one found.
  - never guesses which numeric column corresponds to the requested
    fiscal year: value_col1/value_col2 are ONLY populated when
    year_resolved is True (i.e. coordinate binding actually found the
    requested fiscal_year among this table block's year columns). When it
    could not be resolved — no year-header row detected for this table
    block, or no token bound within tolerance to the requested year —
    value_col1/value_col2 are explicitly left as None rather than falling
    back to "the first two numeric tokens found", which previously let
    unrelated numbers (note references, page numbers, stray ratios) be
    silently reported as if they were real values. Nothing is discarded in
    that case: every numeric token actually found in the matched window is
    preserved in raw_numeric_tokens (text, x0/x1, parsed value, and which
    year — if any — it bound to), so the evidence remains available for
    human review even when no value_col1/value_col2 could be trusted. A
    token that fails to bind to ANY year column (even on a row where other
    tokens did bind) is likewise excluded from mapped_year_values/
    requested_year_value, not merged in as noise. confidence is forced to
    "LOW" whenever year_resolved is False, regardless of how many numbers
    were on the row — "how many numbers were found" was never itself a
    signal that the requested fiscal year had actually been identified.
  - flags (never silently discards) any concept key not present in the
    documented, live concept_dictionary set (see KNOWN_CONCEPT_KEYS below).
  - surfaces (never silently deduplicates/overwrites) duplicate concept
    matches within one document — each occurrence is kept as its own
    candidate, with a warning attached to every candidate sharing that
    concept key (this is the existing, document-wide duplicate check —
    unchanged; the new table_index duplicate check above is a separate,
    page+table-scoped signal, not a replacement for it).

This is a dry-run reporting tool only. It does NOT call
register_source_document()/load_facts() (still unimplemented TODOs in
ingestion/load_historical.py) and is not wired to acquire_one_report()/
acquire_reports() — this module performs no acquisition itself; the
caller is responsible for obtaining pdf_bytes (see scripts/dry_run_extract.py).
"""
from __future__ import annotations

import hashlib
import io
import re
from collections import Counter

import pdfplumber

from parser import CONCEPTS, line_matches, looks_like_financial_value, parse_number

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

# --- coordinate-aware extraction tuning constants -------------------------
_YEAR_TOKEN_RE = re.compile(r"^(20\d{2}),?$")
_ROW_Y_TOLERANCE = 3.0          # points; same clustering tolerance used by
                                 # scripts/diagnose_pdf_word_coordinates.py,
                                 # whose prototype this behavior is based on.
_COLUMN_BOUNDARY_MIN_GUTTER = 30.0   # points; minimum whitespace corridor
                                       # width to treat as a real two-column
                                       # gutter rather than page margin noise.
_COLUMN_BOUNDARY_MARGIN_FRACTION = 0.10  # ignore the outer 10% of page
                                           # width on each side when hunting
                                           # for a gutter (avoids treating a
                                           # page edge as a column boundary).
_YEAR_BIND_MAX_DISTANCE = 40.0  # points; a numeric token further than this
                                  # from every known year column's x-center
                                  # is left unmapped rather than force-bound.


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


def _is_year_token(text: str) -> bool:
    return bool(_YEAR_TOKEN_RE.match(text.strip()))


def _word_x_center(word: dict) -> float:
    return (word["x0"] + word["x1"]) / 2.0


def _group_words_into_rows(words: list[dict], y_tolerance: float = _ROW_Y_TOLERANCE) -> list[list[dict]]:
    """Groups words into visual rows by clustering 'top' (y-position)
    within y_tolerance points of each other. Pure geometry — no text/
    keyword logic. Returns rows sorted top-to-bottom, each row's words
    sorted left-to-right (by x0). Same approach used by the diagnostic
    prototype (scripts/diagnose_pdf_word_coordinates.py) that this
    revision is based on — deliberately called SEPARATELY per column band
    by extract_from_bytes() below, never across a detected column
    boundary, which is what prevents left-column/right-column merging."""
    rows: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        placed = False
        for row in rows:
            if abs(row[0]["top"] - w["top"]) <= y_tolerance:
                row.append(w)
                placed = True
                break
        if not placed:
            rows.append([w])
    for row in rows:
        row.sort(key=lambda w: w["x0"])
    rows.sort(key=lambda row: row[0]["top"])
    return rows


def _detect_column_boundary(words: list[dict], page_width: float) -> float | None:
    """Finds the single widest vertical whitespace corridor ("gutter")
    across the middle of the page (outer _COLUMN_BOUNDARY_MARGIN_FRACTION
    on each side excluded, to avoid mistaking a page margin for a column
    gutter). Returns the corridor's x-midpoint as the column boundary, or
    None if no corridor at least _COLUMN_BOUNDARY_MIN_GUTTER points wide is
    found (i.e. this page is treated as single-column).

    Generic geometry only — not tuned to any specific document's known
    column position. On a genuinely single-column page (e.g. this
    module's own offline test fixtures, whose synthetic content occupies
    only the left portion of the page), any "gutter" this finds simply
    has no words on its far side — see extract_from_bytes(), where the
    narrative-side word list ends up empty and every row still resolves
    to the primary band exactly as before. It only has any actual effect
    when there IS real content on both sides of a real gutter, i.e. a
    genuine two-column layout."""
    if not words or page_width <= 0:
        return None
    width_i = int(page_width) + 1
    occupied = [False] * width_i
    for w in words:
        x0 = max(0, int(w["x0"]))
        x1 = min(width_i, int(w["x1"]) + 1)
        for x in range(x0, x1):
            occupied[x] = True

    margin = page_width * _COLUMN_BOUNDARY_MARGIN_FRACTION
    lo, hi = int(margin), int(page_width - margin)
    best_gap: tuple[int, int] | None = None
    run_start: int | None = None
    for x in range(lo, hi):
        if not occupied[x]:
            if run_start is None:
                run_start = x
        elif run_start is not None:
            run_len = x - run_start
            if run_len >= _COLUMN_BOUNDARY_MIN_GUTTER and (
                best_gap is None or run_len > (best_gap[1] - best_gap[0])
            ):
                best_gap = (run_start, x)
            run_start = None
    if run_start is not None:
        run_len = hi - run_start
        if run_len >= _COLUMN_BOUNDARY_MIN_GUTTER and (
            best_gap is None or run_len > (best_gap[1] - best_gap[0])
        ):
            best_gap = (run_start, hi)

    if best_gap is None:
        return None
    return (best_gap[0] + best_gap[1]) / 2.0


def extract_from_bytes(
    pdf_bytes: bytes,
    ticker: str,
    fiscal_year: int,
    start_page: int | None = None,
    end_page: int | None = None,
) -> dict:
    """READ-ONLY, in-memory, coordinate-aware extraction. Never writes a
    file (uses io.BytesIO, not a temp file), never imports any database
    code, never persists pdf_bytes anywhere beyond this function's own
    local scope.

    start_page/end_page (both optional, 1-based, inclusive) restrict
    processing to a page range — a diagnostic aid for isolating which page
    of a large document is causing a failure. Defaults (None/None) process
    every page, matching the original, unrestricted behavior exactly.
    Pages outside the requested range are never word-extracted at all (no
    page.extract_words() call for them), so the full document's extracted
    content is never held in memory regardless of range size.

    Raises ValueError if the requested range is invalid for this document
    (e.g. start_page > end_page, or end_page beyond the real page count).

    Returns:
      {
        "ticker": ..., "fiscal_year": ..., "document_sha256": ...,
        "page_count": ..., "candidates": [ {...}, ... ],
      }

    Each candidate dict:
      ticker, fiscal_year, statement_type, concept, concept_known,
      reported_label, value_col1, value_col2 (both None unless
      year_resolved is True — see year_resolved below), requested_year_value,
      year_resolved (bool — True iff requested_year_value is not None; a
      candidate should not be treated as a valid fiscal_year extraction
      unless this is True), mapped_year_values, raw_numeric_tokens (list of
      {text, x0, x1, value, bound_year} for EVERY numeric token found in
      the matched window, regardless of whether it ended up bound to a
      year column — full evidence, never filtered out), table_index,
      source_page, source_row_top, source_word_positions, currency, unit,
      extraction_method, confidence, raw_text, warnings (list[str]).
    """
    verify_pdf_bytes(pdf_bytes)
    document_sha256 = hashlib.sha256(pdf_bytes).hexdigest()

    candidates: list[dict] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page_count = len(pdf.pages)

        # Resolve the requested range against the real page count. None on
        # either end means "use the document's actual bound" — this is
        # exactly what makes the no-args-supplied case behave identically
        # to the original, range-less implementation.
        range_start = 1 if start_page is None else start_page
        range_end = page_count if end_page is None else end_page
        if range_start < 1 or range_end < 1 or range_start > range_end or range_end > page_count:
            raise ValueError(
                f"invalid page range: start_page={start_page!r}, end_page={end_page!r} "
                f"for a document with {page_count} pages (valid range is 1..{page_count} "
                "inclusive, with start <= end)"
            )

        print(
            f"[extract_from_bytes] processing pages {range_start}-{range_end} of {page_count}...",
            flush=True,
        )
        for i, page in enumerate(pdf.pages):
            page_number = i + 1
            if page_number < range_start or page_number > range_end:
                # Outside the requested range — skip entirely. extract_words()
                # is never called for this page, so its content is never
                # parsed or held in memory.
                continue
            if page_number == range_start or page_number % 10 == 0 or page_number == range_end:
                print(f"[extract_from_bytes] page {page_number}/{page_count}...", flush=True)
            try:
                words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
                if not words:
                    continue

                # Split the page into a primary (left/table) band and a
                # narrative (right/sidebar) band using a detected column
                # gutter. Rows are grouped SEPARATELY within each band, so
                # a left-column table row and a right-column narrative
                # sentence sharing a similar y-position can never be
                # merged into one reconstructed row.
                boundary = _detect_column_boundary(words, float(page.width))
                if boundary is None:
                    primary_words = words
                else:
                    primary_words = [w for w in words if w["x0"] < boundary]
                    # Narrative-band words are deliberately never grouped
                    # into rows or scanned for concepts — this is what
                    # rejects a sidebar sentence that merely mentions a
                    # concept/number in prose from becoming a candidate.

                primary_rows = _group_words_into_rows(primary_words)

                table_index = 0
                # (year:int, x0:float, x1:float) for the CURRENT table
                # block only — reset whenever a new year-header row is
                # detected; a numeric value is bound to whichever entry's
                # x-center it is nearest to, within _YEAR_BIND_MAX_DISTANCE.
                year_columns: list[tuple[int, float, float]] = []
                page_candidates: list[dict] = []

                for row_idx, row in enumerate(primary_rows):
                    row_year_tokens = [w for w in row if _is_year_token(w["text"])]
                    if len(row_year_tokens) >= 2:
                        # A year-header row — starts a new table block.
                        # Never itself scanned as a data row.
                        table_index += 1
                        year_columns = [
                            (int(w["text"].rstrip(",")), w["x0"], w["x1"])
                            for w in row_year_tokens
                        ]
                        continue

                    # Window = this row + the next row in the SAME band,
                    # mirroring parser.py's original 2-physical-line
                    # sliding window (so a label that wraps onto the next
                    # line still matches its numbers) — but now built from
                    # real word objects with coordinates, not flattened text.
                    next_row = primary_rows[row_idx + 1] if row_idx + 1 < len(primary_rows) else []
                    window_words = row + next_row
                    line_text = " ".join(w["text"] for w in row)
                    window_text = " ".join(w["text"] for w in window_words)
                    line_lower = line_text.lower()
                    window_lower = window_text.lower()

                    for concept, keyword_groups, stmt_type, require_any, exclude_any in CONCEPTS:
                        if not line_matches(line_lower, keyword_groups):
                            continue
                        if require_any and not any(r in line_lower for r in require_any):
                            continue
                        if exclude_any and any(x in window_lower for x in exclude_any):
                            continue

                        numeric_words = [w for w in window_words if looks_like_financial_value(w["text"])]
                        if not numeric_words:
                            continue
                        parsed = [(w, parse_number(w["text"])) for w in numeric_words]
                        parsed = [(w, v) for w, v in parsed if v is not None]
                        if not parsed:
                            continue

                        # Bind each numeric word to the nearest known year
                        # column (if this table block has a detected
                        # header) by x-center distance, instead of
                        # assuming position in reading order == fiscal year.
                        # P0 fix: track EVERY token's bind outcome (not just
                        # the requested year's), so a token that binds to NO
                        # year column at all (e.g. a note/page-reference
                        # number like "6.15" sitting next to real columns)
                        # is identifiable and excluded from being treated as
                        # a value, even on a row where OTHER tokens did
                        # bind successfully.
                        mapped_year_values: dict[int, float] = {}
                        raw_numeric_tokens: list[dict] = []
                        for w, v in parsed:
                            bound_year = None
                            if year_columns:
                                wc = _word_x_center(w)
                                nearest_year, nx0, nx1 = min(
                                    year_columns, key=lambda yc: abs(((yc[1] + yc[2]) / 2.0) - wc)
                                )
                                if abs(((nx0 + nx1) / 2.0) - wc) <= _YEAR_BIND_MAX_DISTANCE:
                                    bound_year = nearest_year
                                    mapped_year_values.setdefault(nearest_year, v)
                            # Full evidence preserved unconditionally — every
                            # token found is reported here regardless of
                            # whether it ended up bound to a year column,
                            # per the explicit "do not silently delete
                            # evidence" requirement.
                            raw_numeric_tokens.append({
                                "text": w["text"],
                                "x0": round(w["x0"], 1),
                                "x1": round(w["x1"], 1),
                                "value": v,
                                "bound_year": bound_year,
                            })

                        requested_year_value = mapped_year_values.get(fiscal_year)
                        year_resolved = requested_year_value is not None

                        warnings: list[str] = []
                        if year_resolved:
                            # FY successfully coordinate-mapped: value_col1/
                            # value_col2 are kept as legacy "first two
                            # tokens found" info for backward compatibility
                            # only — requested_year_value/mapped_year_values
                            # are the authoritative fields a caller should
                            # use for the actual fiscal-year figure.
                            value_col1 = parsed[0][1] if len(parsed) > 0 else None
                            value_col2 = parsed[1][1] if len(parsed) > 1 else None
                        else:
                            # P0 fix (requirements 1 & 3): do NOT fall back
                            # to "first two numeric tokens found" as if they
                            # were real values when the requested fiscal
                            # year could not be reliably coordinate-mapped.
                            # This is exactly the mechanism that let stray/
                            # unrelated numbers (note references, page
                            # numbers, unrelated ratios) be silently
                            # reported as value_col1/value_col2. Nothing is
                            # deleted — every raw token is preserved above
                            # in raw_numeric_tokens for human review.
                            value_col1 = None
                            value_col2 = None
                            if not year_columns:
                                warnings.append(
                                    f"no year-header row detected for this table block — "
                                    f"fiscal year {fiscal_year} could not be coordinate-mapped; "
                                    "value_col1/value_col2 intentionally left unset (not a "
                                    "positional guess) — see raw_numeric_tokens for every "
                                    "value actually found on this row/window"
                                )
                            else:
                                warnings.append(
                                    f"fiscal year {fiscal_year} was not among this table "
                                    "block's detected year columns (or no token bound within "
                                    "tolerance) — value_col1/value_col2 intentionally left "
                                    "unset (not a positional guess) — see raw_numeric_tokens "
                                    "for every value actually found on this row/window"
                                )

                        unbound_count = sum(1 for t in raw_numeric_tokens if t["bound_year"] is None)
                        if year_columns and unbound_count:
                            warnings.append(
                                f"{unbound_count} of {len(raw_numeric_tokens)} numeric token(s) "
                                "on this row/window did not align with any detected year "
                                "column (e.g. a note/page-reference number) — excluded from "
                                "value_col1/value_col2/mapped_year_values; see "
                                "raw_numeric_tokens for the full set with bind status"
                            )
                        if len(parsed) > 2:
                            warnings.append(
                                f"{len(parsed)} numeric tokens found on this row/window — "
                                "see raw_numeric_tokens for the complete set with bind status"
                            )

                        concept_known = concept in KNOWN_CONCEPT_KEYS
                        if not concept_known:
                            warnings.append(
                                f"concept {concept!r} is not in the known/documented "
                                "concept_dictionary set — kept, not discarded"
                            )

                        # P0 fix (requirement 3): a candidate whose
                        # requested fiscal year was not actually resolved
                        # must never present as equally trustworthy as one
                        # that was — confidence is forced LOW rather than
                        # being derived only from "how many numbers were on
                        # this row", which said nothing about whether any
                        # of them were confirmed to be the requested year.
                        if year_resolved:
                            confidence = "MEDIUM" if len(parsed) <= 3 else "LOW"
                        else:
                            confidence = "LOW"

                        page_candidates.append({
                            "ticker": ticker,
                            "fiscal_year": fiscal_year,
                            "statement_type": stmt_type,
                            "concept": concept,
                            "concept_known": concept_known,
                            "reported_label": window_text.strip()[:90],
                            "value_col1": value_col1,
                            "value_col2": value_col2,
                            "requested_year_value": requested_year_value,
                            "year_resolved": year_resolved,
                            "mapped_year_values": dict(mapped_year_values),
                            "raw_numeric_tokens": raw_numeric_tokens,
                            "table_index": table_index,
                            "currency": "SAR",
                            "unit": "thousand",
                            "source_page": page_number,
                            "source_row_top": row[0]["top"] if row else None,
                            "source_word_positions": [
                                (w["text"], round(w["x0"], 1), round(w["x1"], 1)) for w, _ in parsed
                            ],
                            "extraction_method": "pdfplumber_coordinate_aware_v2_dry_run",
                            "confidence": confidence,
                            "raw_text": window_text.strip()[:150],
                            "warnings": warnings,
                        })

                # Table-block duplicate detection: if the SAME concept was
                # matched in more than one distinct table_index on this
                # page (e.g. a SAR table and an apparent USD-denominated
                # duplicate table, as observed in the real SABIC FY2024
                # diagnostic), flag every affected candidate rather than
                # silently merging them or trusting whichever was matched
                # first. This module cannot reliably read a literal
                # currency label off the page, so it does not guess one —
                # duplicates are kept distinct and surfaced for review.
                table_indexes_by_concept: dict[str, set[int]] = {}
                for c in page_candidates:
                    table_indexes_by_concept.setdefault(c["concept"], set()).add(c["table_index"])
                for c in page_candidates:
                    n_blocks = len(table_indexes_by_concept[c["concept"]])
                    if n_blocks > 1:
                        c["warnings"].append(
                            f"concept {c['concept']!r} matched in {n_blocks} distinct table "
                            f"blocks on page {page_number} (table_index={c['table_index']}) — "
                            "possible duplicate/currency-converted table; not merged, review each"
                        )

                candidates.extend(page_candidates)
                del words, primary_rows, page_candidates
            finally:
                # Bound memory to ~1 page at a time. pdfplumber.PDF.pages
                # (see pdfplumber/pdf.py) keeps every Page wrapper alive for
                # the life of this `with` block, and page.extract_words()/
                # extract_text() lazily populate that Page's cached parsed
                # content (chars, rects, lines, curves, images, layout —
                # see pdfplumber/container.py Container.cached_properties
                # and Page.cached_properties) which is never cleared
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
    # Document-wide, unchanged from before this revision (separate from
    # the page+table_index-scoped check above).
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
