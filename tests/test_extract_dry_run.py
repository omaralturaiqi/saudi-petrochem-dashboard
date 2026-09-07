"""
tests/test_extract_dry_run.py

Offline tests for ingestion/extract_dry_run.py. NEVER calls sabic.com,
NEVER calls Neon, NEVER writes a real PDF to data/raw/. Uses a small
synthetic in-memory PDF fixture (built with reportlab, a dev/test-only
dependency — not added to requirements.txt or requirements-ingestion.txt)
so extraction logic is exercised against real pdfplumber parsing without
any network access.
"""
from __future__ import annotations

import ast
import hashlib
import io
import unittest
from pathlib import Path
from unittest.mock import patch

import pdfplumber
from reportlab.pdfgen import canvas

from ingestion.extract_dry_run import (
    KNOWN_CONCEPT_KEYS,
    VALID_PERIOD_TYPES,
    PdfBytesInvalid,
    _detect_period_label_clusters_above,
    _nearest_period_label,
    _select_requested_period_value,
    _split_row_into_period_label_clusters,
    extract_from_bytes,
    verify_pdf_bytes,
)
from scripts.dry_run_extract import build_arg_parser, run as script_run

REPO_ROOT = Path(__file__).resolve().parent.parent

# Real, official SABIC Agri-Nutrients (ticker 2020) interim filing for the
# three-month and six-month periods ended 30 June 2026 — fetched via
# argaamplus.s3.amazonaws.com/4d858ed5-a579-4051-bb24-58dffd007b1d.pdf
# (sabic.com/sabic-agrinutrients.com/saudiexchange.sa are all blocked from
# this sandbox; this S3 mirror was not). Its SHA-256 is asserted in the
# regression test below both as an integrity check on this fixture file
# and as documentation of exactly which real filing produced the expected
# figures asserted there.
REAL_QUARTERLY_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "real_pdfs" / "sabic_agrinutrients_q2_2026.pdf"
REAL_QUARTERLY_FIXTURE_SHA256 = "28aec831b8fbafc31a269fce710db980a524bedf77ec3d01cc11e48ab796d61e"
DATA_RAW_ROOT = REPO_ROOT / "data" / "raw"


def _build_fixture_pdf_bytes() -> bytes:
    """Builds a tiny, real, single-page PDF in memory containing text lines
    designed to exercise: a two-numeric-column match (ambiguity), a
    duplicate concept match (net_income appearing twice), and a
    single-value match. No file is written to disk — canvas draws
    directly into an in-memory BytesIO buffer."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(612, 792))
    c.setFont("Helvetica", 10)
    c.drawString(50, 700, "Total revenue 1,234,567 987,654")
    c.drawString(50, 680, "Net income for the year (259,180) 171,061")
    c.drawString(50, 660, "Net income for the year attributable to Parent 259,180")
    c.showPage()
    c.save()
    return buf.getvalue()


def _build_multi_page_fixture_pdf_bytes(num_pages: int) -> bytes:
    """Builds a small, real, multi-page PDF in memory (each page carrying
    its own text so extract_text() has real per-page content to cache).
    No file is written to disk."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(612, 792))
    for p in range(num_pages):
        c.setFont("Helvetica", 10)
        c.drawString(50, 700, f"Total revenue {1_000_000 + p} 900,000")
        c.drawString(50, 680, f"Net income for the year ({100_000 + p}) 50,000")
        c.showPage()
    c.save()
    return buf.getvalue()


def _build_two_column_fixture_pdf_bytes() -> bytes:
    """Builds a synthetic, real, single-page, TWO-COLUMN PDF: a left-column
    financial table (header row with year tokens, a data row with values
    explicitly positioned under each year column) plus a right-column
    narrative sentence sharing the same y-position as the data row, that
    independently mentions the same concept/number/year in prose. No real
    document values used. Landscape-ish width (792pt) with a >100pt gap
    between the two columns so _detect_column_boundary() finds a real
    gutter."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(792, 612))
    c.setFont("Helvetica", 10)
    # Left column (financial table): header row with year tokens.
    c.drawString(200, 560, "2024")
    c.drawString(260, 560, "2023")
    # Left column data row, values explicitly aligned under their year.
    c.drawString(50, 540, "Total")
    c.drawString(80, 540, "revenue")
    c.drawString(200, 540, "100.00")   # aligned under "2024"
    c.drawString(260, 540, "90.00")    # aligned under "2023"
    # Right column narrative sentence, SAME top as the data row (y=540) —
    # the exact scenario that merged unrelated columns in the real SABIC
    # diagnostic. Independently contains the concept keyword, a number,
    # and a year — if this were captured as its own candidate, or merged
    # into the left row's window, the fix would not be working.
    c.drawString(460, 540, "Total revenue reached 100.00 in 2024 driven by volumes.")
    c.showPage()
    c.save()
    return buf.getvalue()


def _build_duplicate_table_fixture_pdf_bytes() -> bytes:
    """Builds a synthetic, real, single-page PDF with TWO separate,
    vertically-stacked table blocks (each with its own year-header row),
    both containing a net_income match at different magnitudes — modeling
    the real SABIC evidence of the same concept label appearing in what
    looks like a primary table and a separate (e.g. currency-converted)
    duplicate table on the same page. Single-column (no x-gap needed;
    table-block separation here is by y-position / header re-detection,
    not by column)."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(612, 792))
    c.setFont("Helvetica", 10)
    # Table block 1.
    c.drawString(200, 560, "2024")
    c.drawString(260, 560, "2023")
    c.drawString(50, 540, "Net")
    c.drawString(80, 540, "income")
    c.drawString(110, 540, "for")
    c.drawString(130, 540, "the")
    c.drawString(150, 540, "year")
    c.drawString(200, 540, "500.00")   # aligned under block 1's "2024"
    c.drawString(260, 540, "450.00")   # aligned under block 1's "2023"
    # Table block 2 — far enough below (80pt) to be a clearly separate row
    # cluster, with its own fresh year-header row.
    c.drawString(200, 460, "2024")
    c.drawString(260, 460, "2023")
    c.drawString(50, 440, "Net")
    c.drawString(80, 440, "income")
    c.drawString(110, 440, "for")
    c.drawString(130, 440, "the")
    c.drawString(150, 440, "year")
    c.drawString(200, 440, "130.00")   # aligned under block 2's "2024"
    c.drawString(260, 440, "120.00")   # aligned under block 2's "2023"
    c.showPage()
    c.save()
    return buf.getvalue()


class TestCoordinateAwareExtraction(unittest.TestCase):
    """Proves the coordinate-aware rework's specific new behaviors, using
    synthetic (non-SABIC) two-column and duplicate-table fixtures modeled
    on the structural patterns confirmed in the real SABIC FY2024
    diagnostic evidence (cross-column row merging; same concept label
    appearing in two distinct table blocks at different magnitudes)."""

    def test_two_column_page_narrative_is_not_merged_or_captured(self):
        # Requirement: narrative sentences that merely repeat a financial
        # figure must not become financial candidates, and two-column page
        # content must not be cross-merged into one row.
        pdf_bytes = _build_two_column_fixture_pdf_bytes()
        result = extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024)
        revenue_candidates = [c for c in result["candidates"] if c["concept"] == "revenue"]
        self.assertEqual(
            len(revenue_candidates), 1,
            "exactly one revenue candidate expected: the left-column table row only "
            "— the right-column narrative sentence must not produce its own candidate "
            "and must not be merged into the table row's window",
        )
        candidate = revenue_candidates[0]
        # If the narrative sentence's trailing prose ("driven by volumes.")
        # had leaked into this candidate's window, raw_text would contain it.
        self.assertNotIn("driven by volumes", candidate["raw_text"])
        self.assertNotIn("reached", candidate["raw_text"])

    def test_2024_value_explicitly_mapped_via_column_position(self):
        # Requirement: bind numeric tokens to the correct year column using
        # x-position alignment rather than assuming the first value found
        # is the target year. Here "90.00" (2023) is positioned BEFORE
        # "100.00" (2024) would be if this were purely reading-order, but
        # both are drawn in the same left-to-right order as their headers
        # (2024 then 2023) — the test asserts the binding is explicitly
        # tied to header x-position, not merely "first token found".
        pdf_bytes = _build_two_column_fixture_pdf_bytes()
        result = extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024)
        revenue_candidates = [c for c in result["candidates"] if c["concept"] == "revenue"]
        self.assertEqual(len(revenue_candidates), 1)
        candidate = revenue_candidates[0]
        self.assertEqual(candidate["requested_year_value"], 100.00,
                          "the value aligned under the '2024' header must be explicitly mapped")
        self.assertEqual(candidate["mapped_year_values"].get(2024), 100.00)
        self.assertEqual(candidate["mapped_year_values"].get(2023), 90.00)
        # Once coordinate binding resolves the requested year, the old
        # "not disambiguated" warning (which only applies when binding
        # could not resolve it) must not be present.
        self.assertFalse(
            any("not disambiguated" in w for w in candidate["warnings"]),
            "a coordinate-resolved requested_year_value must not also carry the "
            "unresolved-ambiguity warning",
        )

    def test_2023_value_cannot_silently_become_the_2024_value(self):
        # Requesting fiscal_year=2023 against the SAME fixture must resolve
        # to the DIFFERENT value aligned under the "2023" header, proving
        # the binding is genuinely year-specific, not a fixed "first/second
        # token" positional assumption that would return the same number
        # regardless of which year was requested.
        pdf_bytes = _build_two_column_fixture_pdf_bytes()
        result_2024 = extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024)
        result_2023 = extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2023)
        value_2024 = [c for c in result_2024["candidates"] if c["concept"] == "revenue"][0]["requested_year_value"]
        value_2023 = [c for c in result_2023["candidates"] if c["concept"] == "revenue"][0]["requested_year_value"]
        self.assertEqual(value_2024, 100.00)
        self.assertEqual(value_2023, 90.00)
        self.assertNotEqual(value_2024, value_2023)

    def test_duplicate_table_blocks_kept_distinct_and_flagged(self):
        # Requirement: SAR and duplicate (e.g. USD) tables where the same
        # concept label appears at different magnitudes must not be
        # confused/merged — both candidates must be kept, each correctly
        # bound to ITS OWN table block's year columns, and both flagged
        # that a duplicate table block was found for this concept.
        pdf_bytes = _build_duplicate_table_fixture_pdf_bytes()
        result = extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024)
        net_income_candidates = [c for c in result["candidates"] if c["concept"] == "net_income"]
        self.assertEqual(len(net_income_candidates), 2, "both table blocks must produce a separate candidate")

        table_indexes = {c["table_index"] for c in net_income_candidates}
        self.assertEqual(len(table_indexes), 2, "the two candidates must carry distinct table_index values")

        requested_year_values = {c["requested_year_value"] for c in net_income_candidates}
        self.assertEqual(
            requested_year_values, {500.00, 130.00},
            "each candidate's requested_year_value must come from its OWN table block "
            "(500.00 from block 1, 130.00 from block 2) — never cross-contaminated",
        )

        for c in net_income_candidates:
            self.assertTrue(
                any("distinct table" in w for w in c["warnings"]),
                "every candidate sharing this concept across >1 table block on the same "
                "page must be flagged for human review, not silently merged or trusted",
            )


def _build_stray_note_number_fixture_pdf_bytes() -> bytes:
    """Builds a synthetic, real, single-page PDF with a REAL year-header
    row and a data row whose two real values are correctly aligned under
    "2024"/"2023" — plus a THIRD numeric token ("6.15") positioned far
    from every header column (simulating a note/footnote-reference number
    printed near a table row, e.g. "(Note 6.15)"), which must NOT bind to
    any year column. Models the real SABIC evidence of note-reference-
    shaped decimal numbers (e.g. "6.15", "35.2") appearing near genuine
    table values."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(792, 612))
    c.setFont("Helvetica", 10)
    c.drawString(200, 560, "2024")
    c.drawString(260, 560, "2023")
    c.drawString(50, 540, "Total")
    c.drawString(80, 540, "equity")
    c.drawString(200, 540, "300.00")   # aligned under "2024"
    c.drawString(260, 540, "280.00")   # aligned under "2023"
    # Note-reference-shaped number: close enough to the table content that
    # it does NOT open a new column gutter (_detect_column_boundary() picks
    # only the single widest whitespace corridor, which remains the large
    # trailing margin beyond x=340, not this ~60pt internal gap) — but far
    # enough (>> _YEAR_BIND_MAX_DISTANCE=40pt) from both year columns'
    # x-centers (~211 and ~271) that it must not bind to either.
    c.drawString(340, 540, "6.15")
    c.showPage()
    c.save()
    return buf.getvalue()


class TestP0ValueBindingFixes(unittest.TestCase):
    """Proves the P0 fixes approved after the SABIC FY2024 false-positive
    diagnosis: (1) no positional first-two-tokens fallback when the
    requested fiscal year cannot be coordinate-mapped; (2) a numeric token
    that fails to bind to any year column (e.g. a note-reference number)
    is excluded from value_col1/value_col2/mapped_year_values even on an
    otherwise-resolved row; (3) year_resolved is an explicit, reliable
    signal of whether a candidate represents an actual resolved fiscal-
    year value; (4) nothing is silently deleted — raw_numeric_tokens
    preserves every token found, bound or not."""

    def test_stray_unbound_token_excluded_but_preserved_on_resolved_row(self):
        pdf_bytes = _build_stray_note_number_fixture_pdf_bytes()
        result = extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024)
        equity_candidates = [c for c in result["candidates"] if c["concept"] == "total_equity"]
        self.assertEqual(len(equity_candidates), 1)
        c = equity_candidates[0]

        # The row DID resolve the requested year correctly, from the two
        # real, correctly-aligned values.
        self.assertTrue(c["year_resolved"])
        self.assertEqual(c["requested_year_value"], 300.00)
        self.assertEqual(c["mapped_year_values"], {2024: 300.00, 2023: 280.00})

        # The stray "6.15" must never appear as a mapped year value, and
        # must not have silently become value_col2 either (value_col1/
        # value_col2 are the legacy first-two-tokens view — "6.15" is the
        # THIRD token found, so it wouldn't land there positionally in
        # this fixture regardless, but assert the real intended guard:
        # it must be absent from mapped_year_values / requested figures).
        self.assertNotIn(6.15, c["mapped_year_values"].values())
        self.assertNotEqual(c["requested_year_value"], 6.15)

        # Evidence preservation: "6.15" must still be present, verbatim,
        # in raw_numeric_tokens, explicitly marked as NOT bound to any year.
        stray_entries = [t for t in c["raw_numeric_tokens"] if t["value"] == 6.15]
        self.assertEqual(len(stray_entries), 1, "the stray token must be preserved, not deleted")
        self.assertIsNone(stray_entries[0]["bound_year"], "must be recorded as unbound, not guessed")

        # And it must be surfaced in the warnings for human review.
        self.assertTrue(
            any("did not align with any detected year column" in w for w in c["warnings"])
        )

    def test_year_resolved_false_when_no_header_detected_at_all(self):
        # Reuses the existing no-header fixture: confirms year_resolved is
        # explicitly False (not just requested_year_value being None) when
        # there is no year-header row to bind against at all.
        pdf_bytes = _build_fixture_pdf_bytes()
        result = extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024)
        self.assertTrue(result["candidates"])
        for c in result["candidates"]:
            self.assertFalse(c["year_resolved"])
            self.assertIsNone(c["value_col1"])
            self.assertIsNone(c["value_col2"])
            self.assertEqual(c["confidence"], "LOW")


class TestPageMemoryRelease(unittest.TestCase):
    """Proves the memory fix: pdfplumber's per-page cache (chars/objects/
    layout — populated by extract_text()) is released via page.close()
    once extract_from_bytes() is done with a page, rather than being
    retained for every page across the whole document."""

    def test_page_close_called_for_every_page(self):
        # Spies on the real pdfplumber.page.Page.close (still calls the
        # real implementation) to prove the release mechanism actually
        # fires for every page processed, not just some / not zero. Note:
        # page.close() legitimately fires twice per page here — once from
        # our explicit per-page call (which is what bounds memory *during*
        # the loop) and once more, harmlessly, from pdfplumber's own
        # PDF.close() when the `with` block exits (PDF.close() iterates
        # every page and closes it again — see pdfplumber/pdf.py). That
        # second pass is a no-op cleanup on already-empty caches; it does
        # not indicate the fix is missing, so this test only checks that
        # every page number was closed at least once, not an exact count.
        pdf_bytes = _build_multi_page_fixture_pdf_bytes(3)
        original_close = pdfplumber.page.Page.close
        closed_page_numbers = set()

        def spy_close(self, *args, **kwargs):
            closed_page_numbers.add(self.page_number)
            return original_close(self, *args, **kwargs)

        with patch.object(pdfplumber.page.Page, "close", spy_close):
            result = extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024)

        self.assertEqual(result["page_count"], 3)
        self.assertEqual(
            closed_page_numbers, {1, 2, 3},
            "page.close() must be called for every page, proving each "
            "page's cache is released rather than never being cleaned up.",
        )

    def test_previous_pages_cache_cleared_before_next_page_starts(self):
        # The real invariant this fix must satisfy: by the time page N
        # begins extraction, every prior page's cached parsed content
        # (chars/objects/rects/lines/curves/images, tracked via _objects)
        # must already be gone — proving memory is bounded to ~1 page at a
        # time during the loop, not merely cleaned up once at the very end
        # when the `with` block exits.
        pdf_bytes = _build_multi_page_fixture_pdf_bytes(3)
        original_extract_words = pdfplumber.page.Page.extract_words
        pages_seen_so_far = []

        def spy_extract_words(self, *args, **kwargs):
            for prior_page in pages_seen_so_far:
                if hasattr(prior_page, "_objects"):
                    raise AssertionError(
                        f"page {prior_page.page_number}'s cache is still populated "
                        f"while page {self.page_number} is starting extraction — "
                        "memory is not bounded to ~1 page at a time"
                    )
            pages_seen_so_far.append(self)
            return original_extract_words(self, *args, **kwargs)

        with patch.object(pdfplumber.page.Page, "extract_words", spy_extract_words):
            result = extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024)

        self.assertEqual(result["page_count"], 3)
        self.assertEqual(len(pages_seen_so_far), 3)


class TestPageRangeDiagnostic(unittest.TestCase):
    """Proves the --start-page/--end-page diagnostic capability: default
    behavior is unchanged, a supplied range restricts processing to exactly
    those pages (without extracting text for any other page), invalid
    ranges fail cleanly, and all existing guarantees (page.close() cleanup,
    no DB, no filesystem writes) still hold when a range is used."""

    def test_default_behavior_processes_full_document(self):
        # (1) Omitting start_page/end_page must process every page — the
        # exact original, range-less behavior.
        pdf_bytes = _build_multi_page_fixture_pdf_bytes(4)
        result = extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024)
        self.assertEqual(result["page_count"], 4)
        self.assertEqual(
            {c["source_page"] for c in result["candidates"]}, {1, 2, 3, 4},
            "every page must produce candidates when no range is supplied",
        )

    def test_cli_start_end_page_parsed_correctly(self):
        # (2) --start-page/--end-page are recognized CLI arguments, parsed
        # as ints, and default to None (meaning "unrestricted") when
        # omitted — proving the CLI surface itself, independent of
        # extract_from_bytes.
        parser = build_arg_parser()
        args = parser.parse_args(["--start-page", "50", "--end-page", "60"])
        self.assertEqual(args.start_page, 50)
        self.assertEqual(args.end_page, 60)

        args_default = parser.parse_args([])
        self.assertIsNone(args_default.start_page)
        self.assertIsNone(args_default.end_page)

    def test_small_range_processes_only_requested_pages(self):
        # (3) A supplied range must restrict BOTH the returned candidates
        # AND which pages are ever word-extracted at all — proven via a
        # spy on Page.extract_words, not just by checking the output.
        pdf_bytes = _build_multi_page_fixture_pdf_bytes(5)
        original_extract_words = pdfplumber.page.Page.extract_words
        extracted_page_numbers = []

        def spy_extract_words(self, *args, **kwargs):
            extracted_page_numbers.append(self.page_number)
            return original_extract_words(self, *args, **kwargs)

        with patch.object(pdfplumber.page.Page, "extract_words", spy_extract_words):
            result = extract_from_bytes(
                pdf_bytes, ticker="2010", fiscal_year=2024, start_page=2, end_page=3,
            )

        self.assertEqual(result["page_count"], 5, "page_count must still report the real document size")
        self.assertEqual(
            sorted(extracted_page_numbers), [2, 3],
            "extract_words() must be called only for pages within the requested range",
        )
        self.assertEqual(
            {c["source_page"] for c in result["candidates"]}, {2, 3},
            "candidates must come only from pages within the requested range",
        )

    def test_invalid_range_raises_valueerror(self):
        # (4) Invalid ranges (relative to the real page count) must fail
        # cleanly with a clear, descriptive error rather than silently
        # clamping or producing wrong results.
        pdf_bytes = _build_multi_page_fixture_pdf_bytes(3)

        with self.assertRaises(ValueError):
            extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024, start_page=5, end_page=3)  # start > end
        with self.assertRaises(ValueError):
            extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024, start_page=1, end_page=99)  # beyond page_count
        with self.assertRaises(ValueError):
            extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024, start_page=0, end_page=2)  # below 1

    def test_invalid_range_cli_fails_cleanly_with_nonzero_exit_before_fetch(self):
        # (4, CLI level) An obviously-malformed range (start > end, or < 1)
        # must be rejected by scripts/dry_run_extract.py's run() with a
        # non-zero exit code BEFORE any network fetch is attempted — proven
        # by patching requests.get to raise if it's ever called.
        with patch("requests.get", side_effect=AssertionError("requests.get must not be called for an invalid range")):
            with self.assertRaises(SystemExit) as ctx:
                script_run(
                    url="https://example.invalid/never-fetched.pdf",
                    ticker="2010", fiscal_year=2024,
                    start_page=10, end_page=5,  # start > end
                )
            self.assertNotEqual(ctx.exception.code, 0)

    def test_page_close_called_for_every_page_in_range(self):
        # (5) page.close() cleanup must still fire for every page actually
        # processed when a range is supplied. Note: pdfplumber's own
        # PDF.close() (invoked when the `with` block exits) also closes
        # every page in pdf.pages, including pages 1 and 5 which are
        # outside this range — that's a harmless no-op cleanup on pages
        # that were never text-extracted at all (see
        # test_small_range_processes_only_requested_pages, which proves
        # extract_text() is never called for out-of-range pages). So this
        # test checks that the in-range pages are covered, not that the
        # closed set is limited to exactly the range.
        pdf_bytes = _build_multi_page_fixture_pdf_bytes(5)
        original_close = pdfplumber.page.Page.close
        closed_page_numbers = set()

        def spy_close(self, *args, **kwargs):
            closed_page_numbers.add(self.page_number)
            return original_close(self, *args, **kwargs)

        with patch.object(pdfplumber.page.Page, "close", spy_close):
            extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024, start_page=2, end_page=4)

        self.assertTrue(
            {2, 3, 4}.issubset(closed_page_numbers),
            "page.close() must be called for every page within the requested range "
            f"(got {sorted(closed_page_numbers)})",
        )

    def test_ranged_extraction_has_no_db_imports_and_no_filesystem_writes(self):
        # (6) Existing read-only/in-memory guarantees must hold when a
        # range is supplied too — no write-mode open() call anywhere in
        # the call graph, and no new files under data/raw/.
        import builtins
        real_open = builtins.open

        def guarded_open(file, mode="r", *args, **kwargs):
            if any(m in mode for m in ("w", "a", "x")):
                raise AssertionError(f"unexpected write-mode open() call: {file!r} mode={mode!r}")
            return real_open(file, mode, *args, **kwargs)

        before = set(DATA_RAW_ROOT.rglob("*")) if DATA_RAW_ROOT.exists() else set()
        pdf_bytes = _build_multi_page_fixture_pdf_bytes(3)
        with patch("builtins.open", side_effect=guarded_open):
            extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024, start_page=1, end_page=2)
        after = set(DATA_RAW_ROOT.rglob("*")) if DATA_RAW_ROOT.exists() else set()
        self.assertEqual(before, after)
        # The no-DB-import AST check already covers both files' full source
        # (see TestNoDatabaseImports) — that check is source-level and does
        # not need to be re-run per code path, since it verifies no such
        # import statement exists anywhere in either file at all.


class TestVerifyPdfBytes(unittest.TestCase):
    def test_valid_pdf_magic_bytes_pass(self):
        pdf_bytes = _build_fixture_pdf_bytes()
        verify_pdf_bytes(pdf_bytes)  # should not raise

    def test_empty_bytes_rejected(self):
        with self.assertRaises(PdfBytesInvalid):
            verify_pdf_bytes(b"")

    def test_malformed_non_pdf_bytes_rejected(self):
        with self.assertRaises(PdfBytesInvalid):
            verify_pdf_bytes(b"<html><body>not a pdf, an error page</body></html>")

    def test_truncated_pdf_header_rejected(self):
        with self.assertRaises(PdfBytesInvalid):
            verify_pdf_bytes(b"%PD")  # too short to be the real magic bytes


class TestExtractFromBytesOpensViaBytesIO(unittest.TestCase):
    def test_valid_pdf_bytes_open_successfully(self):
        # (A) valid PDF bytes can be opened from BytesIO / extraction runs
        # without raising.
        pdf_bytes = _build_fixture_pdf_bytes()
        result = extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024)
        self.assertIn("candidates", result)
        self.assertGreater(result["page_count"], 0)

    def test_malformed_bytes_raise_before_any_pdfplumber_call(self):
        # (B) malformed/truncated bytes are rejected — raised by
        # verify_pdf_bytes() before pdfplumber.open() is ever reached.
        with self.assertRaises(PdfBytesInvalid):
            extract_from_bytes(b"not a real pdf at all", ticker="2010", fiscal_year=2024)


class TestSha256(unittest.TestCase):
    def test_sha256_computed_directly_from_bytes(self):
        # (C) SHA-256 is calculated from bytes, matching hashlib on the
        # exact same bytes passed in (no re-read from any file).
        pdf_bytes = _build_fixture_pdf_bytes()
        expected = hashlib.sha256(pdf_bytes).hexdigest()
        result = extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024)
        self.assertEqual(result["document_sha256"], expected)


class TestExtractionReturnsCandidates(unittest.TestCase):
    def test_returns_candidate_dictionaries_with_expected_keys(self):
        # (D) extraction returns candidate dictionaries with the required
        # metadata fields.
        pdf_bytes = _build_fixture_pdf_bytes()
        result = extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024)
        self.assertGreater(len(result["candidates"]), 0)
        required_keys = {
            "ticker", "fiscal_year", "statement_type", "concept", "concept_known",
            "reported_label", "value_col1", "value_col2", "currency", "unit",
            "source_page", "extraction_method", "confidence", "raw_text", "warnings",
        }
        for candidate in result["candidates"]:
            self.assertTrue(required_keys.issubset(candidate.keys()))
            self.assertEqual(candidate["ticker"], "2010")
            self.assertEqual(candidate["fiscal_year"], 2024)


class TestUnknownConceptFlagging(unittest.TestCase):
    def test_concept_not_in_known_set_is_flagged_not_dropped(self):
        # (E) unknown concepts are explicitly flagged (concept_known=False
        # + a warning) rather than silently discarded. Simulate "unknown"
        # by shrinking the known-concepts set to exclude net_income, which
        # the fixture is guaranteed to match.
        pdf_bytes = _build_fixture_pdf_bytes()
        shrunk_known_set = KNOWN_CONCEPT_KEYS - {"net_income"}
        with patch("ingestion.extract_dry_run.KNOWN_CONCEPT_KEYS", shrunk_known_set):
            result = extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024)
        net_income_candidates = [c for c in result["candidates"] if c["concept"] == "net_income"]
        self.assertTrue(net_income_candidates, "fixture must produce at least one net_income candidate")
        for c in net_income_candidates:
            self.assertFalse(c["concept_known"])
            self.assertTrue(any("not in the known" in w for w in c["warnings"]))
        # The candidate is still present in the results, not dropped.
        self.assertIn("net_income", {c["concept"] for c in result["candidates"]})


class TestAmbiguityWarning(unittest.TestCase):
    def test_unresolved_multiple_numeric_columns_leave_value_cols_unset_not_guessed(self):
        # (F, P0-updated) _build_fixture_pdf_bytes() has NO year-header row
        # at all, so the requested fiscal year can never be coordinate-
        # mapped for this fixture. Per the P0 fix: when the year cannot be
        # resolved, value_col1/value_col2 must be left unset (None) rather
        # than falling back to "the first two numeric tokens found" as if
        # they were real values — but NOTHING is discarded: every token
        # actually found must still be present in raw_numeric_tokens.
        pdf_bytes = _build_fixture_pdf_bytes()
        result = extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024)
        revenue_candidates = [c for c in result["candidates"] if c["concept"] == "revenue"]
        self.assertTrue(revenue_candidates, "fixture must produce a revenue candidate")
        for c in revenue_candidates:
            self.assertFalse(c["year_resolved"])
            self.assertIsNone(c["requested_year_value"])
            self.assertIsNone(c["value_col1"], "must not fall back to a positional guess")
            self.assertIsNone(c["value_col2"], "must not fall back to a positional guess")
            self.assertEqual(c["confidence"], "LOW", "an unresolved year must never present as MEDIUM")
            # Evidence preservation: every numeric token actually found in
            # this candidate's window must still be present, verbatim, in
            # raw_numeric_tokens. The window is this row + the next row
            # (mirroring parser.py's original 2-line label-wrap design —
            # see extract_from_bytes()), so the revenue row's window also
            # includes the following "Net income..." row's two numbers.
            raw_values = {t["value"] for t in c["raw_numeric_tokens"]}
            self.assertEqual(raw_values, {1234567.0, 987654.0, -259180.0, 171061.0})
            self.assertTrue(
                any("could not be coordinate-mapped" in w for w in c["warnings"]),
            )


class TestDuplicateDetection(unittest.TestCase):
    def test_duplicate_concept_matches_are_surfaced_not_merged(self):
        # (G) the fixture has TWO lines that both match "net_income" —
        # both must be kept as separate candidates (never silently
        # deduplicated/overwritten), each carrying a duplicate warning.
        pdf_bytes = _build_fixture_pdf_bytes()
        result = extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024)
        net_income_candidates = [c for c in result["candidates"] if c["concept"] == "net_income"]
        self.assertGreaterEqual(len(net_income_candidates), 2, "fixture must trigger a duplicate net_income match")
        for c in net_income_candidates:
            self.assertTrue(any("detected" in w and "times" in w for w in c["warnings"]))


class TestSourcePageRetained(unittest.TestCase):
    def test_source_page_present_and_correct(self):
        # (H) every candidate retains its real source_page for traceability.
        pdf_bytes = _build_fixture_pdf_bytes()
        result = extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024)
        self.assertTrue(result["candidates"])
        for c in result["candidates"]:
            self.assertEqual(c["source_page"], 1)  # single-page fixture
            self.assertTrue(c["raw_text"])


class TestNoDatabaseImports(unittest.TestCase):
    def test_module_source_contains_no_database_imports(self):
        # (I) no database imports/connections are used — checked
        # structurally against the actual source text of both new files,
        # not just by convention.
        # AST-based, not a naive substring search — the docstrings in both
        # files legitimately SAY "no psycopg" / "no NEON_CONNECTION_STRING"
        # in prose explaining what is deliberately absent; a substring
        # check would (and initially did) false-positive on that prose.
        # This checks actual import statements only.
        forbidden_modules = {"psycopg", "psycopg2", "us_xbrl_api", "app"}
        forbidden_names = {"NEON_CONNECTION_STRING"}
        for rel_path in ("ingestion/extract_dry_run.py", "scripts/dry_run_extract.py"):
            source = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=rel_path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root_module = alias.name.split(".")[0]
                        self.assertNotIn(root_module, forbidden_modules,
                                          f"{rel_path} must not import {alias.name!r}")
                elif isinstance(node, ast.ImportFrom):
                    module = (node.module or "").split(".")[0]
                    self.assertNotIn(module, forbidden_modules,
                                      f"{rel_path} must not import from {node.module!r}")
                    for alias in node.names:
                        self.assertNotIn(alias.name, forbidden_names,
                                          f"{rel_path} must not import {alias.name!r}")

    def test_module_does_not_import_load_historical(self):
        # AST-based: the module's own docstring legitimately explains it
        # does NOT import load_historical — that prose mention is fine;
        # only an actual import statement referencing it is disallowed.
        source = (REPO_ROOT / "scripts" / "dry_run_extract.py").read_text(encoding="utf-8")
        tree = ast.parse(source, filename="scripts/dry_run_extract.py")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn("load_historical", alias.name)
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn("load_historical", node.module or "")


class TestNoFilesystemPersistence(unittest.TestCase):
    def test_extraction_creates_no_new_files_under_data_raw(self):
        # (J) no filesystem PDF persistence occurs — confirmed by snapshotting
        # data/raw/ before and after running extraction on fixture bytes.
        before = set(DATA_RAW_ROOT.rglob("*")) if DATA_RAW_ROOT.exists() else set()
        pdf_bytes = _build_fixture_pdf_bytes()
        extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024)
        after = set(DATA_RAW_ROOT.rglob("*")) if DATA_RAW_ROOT.exists() else set()
        self.assertEqual(before, after, "extract_from_bytes() must not create/modify any file under data/raw/")

    def test_extraction_does_not_call_builtin_open_in_write_mode(self):
        # Extra structural guarantee: patch builtins.open to raise if ever
        # invoked in a write mode during extraction — proves no accidental
        # file write path exists in the call graph.
        import builtins
        real_open = builtins.open

        def guarded_open(file, mode="r", *args, **kwargs):
            if any(m in mode for m in ("w", "a", "x")):
                raise AssertionError(f"unexpected write-mode open() call: {file!r} mode={mode!r}")
            return real_open(file, mode, *args, **kwargs)

        pdf_bytes = _build_fixture_pdf_bytes()
        with patch("builtins.open", side_effect=guarded_open):
            extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024)


class TestQuarterlyPeriodMetadata(unittest.TestCase):
    """Proves the approved, minimal quarterly-ingestion interface change:
    period_type/fiscal_quarter are accepted, validated, and propagated
    onto every candidate and the returned summary dict — as REQUEST
    LABELS ONLY. No quarter-specific column-header detection exists (see
    TestQuarterlyHeaderDetectionNotImplemented below for why, and the
    explicit stop this session reported instead of inventing one) — a
    "Q1" request must bind against bare-year header columns EXACTLY like
    an "FY" request for the same document/fiscal_year, proven here by
    running both and asserting byte-for-byte-equal binding results."""

    def test_default_period_type_is_fy_unchanged_from_before(self):
        # Backward compatibility: omitting period_type/fiscal_quarter
        # entirely must behave identically to every pre-existing call
        # site in this test file (all of which call extract_from_bytes()
        # with no period_type/fiscal_quarter argument at all).
        pdf_bytes = _build_fixture_pdf_bytes()
        result = extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024)
        self.assertEqual(result["period_type"], "FY")
        self.assertIsNone(result["fiscal_quarter"])
        for c in result["candidates"]:
            self.assertEqual(c["period_type"], "FY")
            self.assertIsNone(c["fiscal_quarter"])

    def test_q1_request_propagates_metadata_onto_every_candidate_and_summary(self):
        pdf_bytes = _build_two_column_fixture_pdf_bytes()
        result = extract_from_bytes(
            pdf_bytes, ticker="2010", fiscal_year=2024,
            period_type="Q1", fiscal_quarter=1,
        )
        self.assertEqual(result["period_type"], "Q1")
        self.assertEqual(result["fiscal_quarter"], 1)
        self.assertTrue(result["candidates"], "fixture must still produce candidates")
        for c in result["candidates"]:
            self.assertEqual(c["period_type"], "Q1")
            self.assertEqual(c["fiscal_quarter"], 1)

    def test_q1_request_binds_identically_to_fy_request_same_document(self):
        # The core honesty check: requesting a quarter must NOT silently
        # change binding behavior, since no quarter-column detection
        # exists. Compare every field except the period_type/
        # fiscal_quarter labels themselves — everything else (value_col1/
        # value_col2, requested_year_value, year_resolved,
        # mapped_year_values, raw_numeric_tokens, table_index, warnings)
        # must be identical between an FY and a Q1 request against the
        # exact same bytes/fiscal_year.
        pdf_bytes = _build_two_column_fixture_pdf_bytes()
        fy_result = extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024)
        q1_result = extract_from_bytes(
            pdf_bytes, ticker="2010", fiscal_year=2024,
            period_type="Q1", fiscal_quarter=1,
        )
        self.assertEqual(len(fy_result["candidates"]), len(q1_result["candidates"]))
        for fy_c, q1_c in zip(fy_result["candidates"], q1_result["candidates"]):
            fy_stripped = {k: v for k, v in fy_c.items() if k not in ("period_type", "fiscal_quarter")}
            q1_stripped = {k: v for k, v in q1_c.items() if k not in ("period_type", "fiscal_quarter")}
            self.assertEqual(
                fy_stripped, q1_stripped,
                "requesting period_type='Q1' must not change ANY binding/extraction "
                "behavior versus period_type='FY' for the same document — only the "
                "label differs, since quarter-column detection is not implemented",
            )

    def test_invalid_period_type_rejected(self):
        pdf_bytes = _build_fixture_pdf_bytes()
        with self.assertRaises(ValueError):
            extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024, period_type="Q5")

    def test_invalid_fiscal_quarter_rejected(self):
        pdf_bytes = _build_fixture_pdf_bytes()
        with self.assertRaises(ValueError):
            extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024, fiscal_quarter=5)

    def test_fiscal_quarter_not_cross_validated_against_period_type(self):
        # Documented, deliberate design choice (see extract_from_bytes()'s
        # own docstring): fiscal_quarter=2 with period_type="FY" is
        # ACCEPTED, not rejected — mirrors schema.sql's own independently-
        # nullable/CHECKed columns, not a compound constraint.
        pdf_bytes = _build_fixture_pdf_bytes()
        result = extract_from_bytes(
            pdf_bytes, ticker="2010", fiscal_year=2024,
            period_type="FY", fiscal_quarter=2,
        )
        self.assertEqual(result["period_type"], "FY")
        self.assertEqual(result["fiscal_quarter"], 2)

    def test_all_valid_period_types_accepted(self):
        pdf_bytes = _build_fixture_pdf_bytes()
        for pt in VALID_PERIOD_TYPES:
            result = extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024, period_type=pt)
            self.assertEqual(result["period_type"], pt)


class TestQuarterlyDoesNotBreakTableIndexOrP0Fixes(unittest.TestCase):
    """Proves the two hard non-goals of this task: (1) table_index/
    duplicate-block logic is untouched by a quarterly-labeled request,
    even on a document with FY-shaped year-header tables (the only shape
    this engine can detect) appearing more than once; (2) the P0 value-
    binding fixes from 5f6791f (no positional fallback, unresolved
    mapping stays unresolved, raw_numeric_tokens always preserved, an
    unbound token never becomes authoritative) hold identically under a
    quarterly-labeled request."""

    def test_duplicate_table_blocks_unaffected_by_period_type_label(self):
        pdf_bytes = _build_duplicate_table_fixture_pdf_bytes()
        fy_result = extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024)
        q1_result = extract_from_bytes(
            pdf_bytes, ticker="2010", fiscal_year=2024,
            period_type="Q1", fiscal_quarter=1,
        )
        fy_table_indexes = sorted({c["table_index"] for c in fy_result["candidates"]})
        q1_table_indexes = sorted({c["table_index"] for c in q1_result["candidates"]})
        self.assertEqual(fy_table_indexes, q1_table_indexes)
        self.assertEqual(len(fy_table_indexes), 2, "fixture defines exactly 2 distinct table blocks")
        # The duplicate-table-block warning must still fire identically.
        fy_dup_warned = [c for c in fy_result["candidates"]
                          if any("distinct table" in w for w in c["warnings"])]
        q1_dup_warned = [c for c in q1_result["candidates"]
                          if any("distinct table" in w for w in c["warnings"])]
        self.assertEqual(len(fy_dup_warned), len(q1_dup_warned))
        self.assertTrue(fy_dup_warned, "the duplicate-table-block warning must still fire")

    def test_p0_stray_unbound_token_behavior_unaffected_by_period_type_label(self):
        pdf_bytes = _build_stray_note_number_fixture_pdf_bytes()
        result = extract_from_bytes(
            pdf_bytes, ticker="2010", fiscal_year=2024,
            period_type="Q1", fiscal_quarter=1,
        )
        equity_candidates = [c for c in result["candidates"] if c["concept"] == "total_equity"]
        self.assertEqual(len(equity_candidates), 1)
        c = equity_candidates[0]

        # Same P0 assertions as TestP0ValueBindingFixes, now under a
        # quarterly-labeled request — must hold identically.
        self.assertTrue(c["year_resolved"])
        self.assertEqual(c["requested_year_value"], 300.00)
        self.assertNotIn(6.15, c["mapped_year_values"].values())
        stray_entries = [t for t in c["raw_numeric_tokens"] if t["value"] == 6.15]
        self.assertEqual(len(stray_entries), 1, "the stray token must still be preserved, not deleted")
        self.assertIsNone(stray_entries[0]["bound_year"], "must still be recorded as unbound, not guessed")
        self.assertTrue(
            any("did not align with any detected year column" in w for w in c["warnings"])
        )

    def test_p0_no_positional_fallback_unaffected_by_period_type_label(self):
        # Reuses the no-year-header fixture: even under a quarterly label,
        # a document with NO detectable year-header row must still leave
        # year_resolved False and value_col1/value_col2 unset — never a
        # positional guess, regardless of what period was requested.
        pdf_bytes = _build_fixture_pdf_bytes()
        result = extract_from_bytes(
            pdf_bytes, ticker="2010", fiscal_year=2024,
            period_type="Q1", fiscal_quarter=1,
        )
        self.assertTrue(result["candidates"])
        for c in result["candidates"]:
            self.assertFalse(c["year_resolved"])
            self.assertIsNone(c["value_col1"])
            self.assertIsNone(c["value_col2"])
            self.assertEqual(c["confidence"], "LOW")


class TestQuarterlyHeaderDetectionNotImplemented(unittest.TestCase):
    """UPDATED — the blocker this class originally recorded (no real
    quarterly financial-report PDF/header available anywhere) has since
    been resolved: a real SABIC Agri-Nutrients Q2 2026 interim filing was
    obtained via argaamplus.s3.amazonaws.com (sabic.com/
    sabic-agrinutrients.com/saudiexchange.sa remain blocked from this
    sandbox — that S3 mirror was not) and used to build
    TestPeriodBlockDisambiguation* below, which supersedes this class for
    the two phrases actually observed ("three-month"/"six-month").

    What remains genuinely NOT implemented, and is still recorded here
    rather than silently having no test for it: any period-label wording
    OTHER than those two exact phrases — e.g. "nine-month"/"twelve-month"
    cumulative labels, an "H2" filing's own wording (never observed), or
    a header shape entirely unlike "For the <N>-month period ended
    <date>" (e.g. a literal "Q1 2025" column caption). Per explicit
    instruction, none of that is guessed or implemented without
    inspecting a real example first."""

    def test_skipped_pending_additional_real_period_label_wording(self):
        self.skipTest(
            "BLOCKED (narrowed from the original, now-resolved blocker): 'three-month'/"
            "'six-month' period-label detection IS implemented and tested against a real "
            "filing (see TestPeriodBlockDisambiguation* below). Still blocked: any OTHER "
            "period-label wording (nine-month/twelve-month cumulative labels, a real H2 "
            "filing's own wording, or a differently-shaped header e.g. a literal 'Q1 2025' "
            "column caption) — none of these has been observed in a real filing this "
            "session, so none is guessed or implemented. This test exists to keep that "
            "narrower remaining gap visible, not to silently omit it."
        )


class TestPeriodLabelHelperFunctions(unittest.TestCase):
    """Pure, offline, no-PDF unit tests for the 4 new helper functions
    this fix adds: _split_row_into_period_label_clusters(),
    _detect_period_label_clusters_above(), _nearest_period_label(), and
    _select_requested_period_value(). Exercises them directly against
    hand-built word/row data (not a rendered PDF) for fast, precise
    coverage of the exact collision this fix targets, independent of any
    real or synthetic PDF fixture."""

    def _word(self, text, x0, x1):
        return {"text": text, "x0": x0, "x1": x1, "top": 0.0}

    def test_split_row_finds_the_widest_gap(self):
        # Mirrors the real header's shape: "For the three-month period"
        # (tightly spaced words) ... a wide gap ... "For the six-month
        # period" (tightly spaced words).
        row = [
            self._word("For", 100, 120), self._word("the", 124, 140),
            self._word("three-month", 144, 210), self._word("period", 214, 260),
            self._word("For", 400, 420), self._word("the", 424, 440),
            self._word("six-month", 444, 500), self._word("period", 504, 550),
        ]
        clusters = _split_row_into_period_label_clusters(row)
        self.assertEqual(len(clusters), 2)
        self.assertEqual(clusters[0]["text"], "for the three-month period")
        self.assertEqual(clusters[1]["text"], "for the six-month period")
        self.assertEqual(clusters[0]["x0"], 100)
        self.assertEqual(clusters[0]["x1"], 260)
        self.assertEqual(clusters[1]["x0"], 400)
        self.assertEqual(clusters[1]["x1"], 550)

    def test_split_row_fewer_than_two_words_returns_empty(self):
        self.assertEqual(_split_row_into_period_label_clusters([]), [])
        self.assertEqual(_split_row_into_period_label_clusters([self._word("x", 0, 10)]), [])

    def test_detect_period_label_clusters_above_finds_the_real_observed_row(self):
        # Mirrors the real filing's exact 3-row structure: period-label
        # row, then a "Notes ended ..." row (no keyword match — must be
        # skipped over, not mistaken for the label row), then the bare-
        # year header row itself (index passed in as header_row_idx).
        primary_rows = [
            [self._word("For", 100, 120), self._word("three-month", 144, 210), self._word("period", 214, 260),
             self._word("For", 400, 420), self._word("six-month", 444, 500), self._word("period", 504, 550)],
            [self._word("Notes", 100, 130), self._word("ended", 134, 170),
             self._word("ended", 400, 436)],
            [self._word("2026", 190, 220), self._word("2025", 260, 290),
             self._word("2026", 490, 520), self._word("2025", 560, 590)],
        ]
        clusters = _detect_period_label_clusters_above(primary_rows, header_row_idx=2)
        self.assertEqual(len(clusters), 2)
        labels = {c["period_label"] for c in clusters}
        self.assertEqual(labels, {"three-month", "six-month"})

    def test_detect_period_label_clusters_above_returns_empty_when_no_keyword_row_found(self):
        # A normal annual-report-style block: nothing but a bare-year
        # header row, nothing resembling a period-label row above it —
        # must return [] (preserving today's exact pre-fix behavior).
        primary_rows = [
            [self._word("Total", 50, 80), self._word("revenue", 84, 130)],
            [self._word("2024", 190, 220), self._word("2023", 260, 290)],
        ]
        clusters = _detect_period_label_clusters_above(primary_rows, header_row_idx=1)
        self.assertEqual(clusters, [])

    def test_nearest_period_label_inside_range(self):
        clusters = [
            {"x0": 100, "x1": 260, "period_label": "three-month"},
            {"x0": 400, "x1": 550, "period_label": "six-month"},
        ]
        self.assertEqual(_nearest_period_label(clusters, 205.0), "three-month")
        self.assertEqual(_nearest_period_label(clusters, 475.0), "six-month")

    def test_nearest_period_label_outside_every_range_picks_closest(self):
        clusters = [
            {"x0": 100, "x1": 260, "period_label": "three-month"},
            {"x0": 400, "x1": 550, "period_label": "six-month"},
        ]
        self.assertEqual(_nearest_period_label(clusters, 300.0), "three-month")  # 40 away vs 100 away
        self.assertEqual(_nearest_period_label(clusters, 370.0), "six-month")    # 30 away vs 110 away

    def test_nearest_period_label_empty_clusters_returns_none(self):
        self.assertIsNone(_nearest_period_label([], 200.0))

    def test_select_requested_period_value_unambiguous_single_match_ignores_period_type(self):
        # Exactly 1 match for the year — resolved regardless of
        # period_type, INCLUDING when period_label is None (the normal
        # single-period-block/annual case) — this is what preserves
        # byte-for-byte pre-fix behavior for every non-ambiguous table.
        mapped = {(2024, None): 300.0, (2023, None): 280.0}
        self.assertEqual(_select_requested_period_value(mapped, 2024, "FY"), 300.0)
        self.assertEqual(_select_requested_period_value(mapped, 2024, "Q2"), 300.0)  # period_type irrelevant here

    def test_select_requested_period_value_ambiguous_resolves_via_mapped_period_type(self):
        mapped = {
            (2026, "three-month"): 712192.0, (2025, "three-month"): 1266945.0,
            (2026, "six-month"): 2080959.0, (2025, "six-month"): 2432338.0,
        }
        self.assertEqual(_select_requested_period_value(mapped, 2026, "Q2"), 712192.0)
        self.assertEqual(_select_requested_period_value(mapped, 2026, "H1"), 2080959.0)
        self.assertEqual(_select_requested_period_value(mapped, 2025, "Q3"), 1266945.0)  # any QN -> three-month

    def test_select_requested_period_value_ambiguous_unmapped_period_type_refuses_to_guess(self):
        mapped = {(2026, "three-month"): 712192.0, (2026, "six-month"): 2080959.0}
        # FY and H2 have no _PERIOD_TYPE_TO_PERIOD_LABEL entry — must
        # refuse (None), never silently pick either block.
        self.assertIsNone(_select_requested_period_value(mapped, 2026, "FY"))
        self.assertIsNone(_select_requested_period_value(mapped, 2026, "H2"))

    def test_select_requested_period_value_no_match_for_year_returns_none(self):
        mapped = {(2026, "three-month"): 712192.0}
        self.assertIsNone(_select_requested_period_value(mapped, 2099, "Q2"))


def _build_period_block_fixture_pdf_bytes() -> bytes:
    """Builds a synthetic, real, single-page PDF mirroring the ACTUAL
    structure observed in the real SABIC Agri-Nutrients Q2 2026 filing's
    income statement (not invented beyond it): a period-label row ("For
    the three-month period" / "For the six-month period"), a bare-year
    header row with the SAME years repeated per block (2025/2024 under
    each), and one data row (Revenue) with 4 numeric columns:
    Q2 2025 | Q2 2024 | H1 2025 | H1 2024 — the exact shape named in this
    task's own test requirements."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(792, 612))
    c.setFont("Helvetica", 10)
    # Title line spanning (near) the full page width, exactly as a real
    # statement header does — this is what keeps _detect_column_boundary()
    # from mistaking the horizontal gap between the three-month and
    # six-month blocks below for a genuine two-column page gutter (the
    # real filing's page is filled with comparable full-width content;
    # without this line the two blocks would be >30pt apart with nothing
    # else on the page, which would falsely trigger the two-column split
    # this module's OWN _detect_column_boundary() correctly performs for
    # actual two-column pages — see TestCoordinateAwareExtraction).
    c.drawString(50, 580, "Interim condensed statement of income for the six-month period ended 30 June (SAR '000)")
    c.drawString(150, 560, "For the three-month period")
    c.drawString(450, 560, "For the six-month period")
    c.drawString(160, 540, "2025")
    c.drawString(220, 540, "2024")
    c.drawString(460, 540, "2025")
    c.drawString(520, 540, "2024")
    c.drawString(50, 520, "Total revenue")
    c.drawString(160, 520, "100.00")   # Q2 2025 (three-month)
    c.drawString(220, 520, "90.00")    # Q2 2024 (three-month)
    c.drawString(460, 520, "250.00")   # H1 2025 (six-month, YTD)
    c.drawString(520, 520, "230.00")   # H1 2024 (six-month, YTD)
    c.showPage()
    c.save()
    return buf.getvalue()


class TestPeriodBlockDisambiguationSyntheticFixture(unittest.TestCase):
    """Proves period-block disambiguation end-to-end against a synthetic,
    from-scratch PDF built to mirror the real observed 4-column shape
    (Q2 2025 | Q2 2024 | H1 2025 | H1 2024) — independent of the real PDF
    fixture (which requires the checked-in binary file to be present;
    this test always runs)."""

    def test_q2_selects_three_month_block(self):
        pdf_bytes = _build_period_block_fixture_pdf_bytes()
        result = extract_from_bytes(pdf_bytes, ticker="2020", fiscal_year=2025, period_type="Q2", fiscal_quarter=2)
        revenue = [c for c in result["candidates"] if c["concept"] == "revenue"][0]
        self.assertTrue(revenue["year_resolved"])
        self.assertEqual(revenue["requested_year_value"], 100.00)
        self.assertEqual(
            revenue["mapped_period_year_values"],
            {"2025:three-month": 100.00, "2024:three-month": 90.00,
             "2025:six-month": 250.00, "2024:six-month": 230.00},
        )

    def test_h1_selects_six_month_block(self):
        pdf_bytes = _build_period_block_fixture_pdf_bytes()
        result = extract_from_bytes(pdf_bytes, ticker="2020", fiscal_year=2025, period_type="H1")
        revenue = [c for c in result["candidates"] if c["concept"] == "revenue"][0]
        self.assertTrue(revenue["year_resolved"])
        self.assertEqual(revenue["requested_year_value"], 250.00)

    def test_comparative_year_also_disambiguated_correctly(self):
        # The comparative year (2024) is ALSO ambiguous across both
        # blocks — proves this isn't specific to the "current" year.
        pdf_bytes = _build_period_block_fixture_pdf_bytes()
        q2_result = extract_from_bytes(pdf_bytes, ticker="2020", fiscal_year=2024, period_type="Q2", fiscal_quarter=2)
        h1_result = extract_from_bytes(pdf_bytes, ticker="2020", fiscal_year=2024, period_type="H1")
        q2_revenue = [c for c in q2_result["candidates"] if c["concept"] == "revenue"][0]
        h1_revenue = [c for c in h1_result["candidates"] if c["concept"] == "revenue"][0]
        self.assertEqual(q2_revenue["requested_year_value"], 90.00)
        self.assertEqual(h1_revenue["requested_year_value"], 230.00)

    def test_unmapped_period_type_does_not_guess(self):
        # FY has no _PERIOD_TYPE_TO_PERIOD_LABEL entry — against this
        # genuinely ambiguous document, it must stay unresolved rather
        # than silently picking either block.
        pdf_bytes = _build_period_block_fixture_pdf_bytes()
        result = extract_from_bytes(pdf_bytes, ticker="2020", fiscal_year=2025, period_type="FY")
        revenue = [c for c in result["candidates"] if c["concept"] == "revenue"][0]
        self.assertFalse(revenue["year_resolved"])
        self.assertIsNone(revenue["requested_year_value"])

    def test_raw_numeric_tokens_preserves_all_four_values_with_bound_period_label(self):
        # The audit trail must show ALL four numbers, each correctly
        # tagged with bound_year AND the new bound_period_label field —
        # regardless of which one ends up selected as requested_year_value.
        pdf_bytes = _build_period_block_fixture_pdf_bytes()
        result = extract_from_bytes(pdf_bytes, ticker="2020", fiscal_year=2025, period_type="Q2", fiscal_quarter=2)
        revenue = [c for c in result["candidates"] if c["concept"] == "revenue"][0]
        tokens = {(t["value"], t["bound_year"], t["bound_period_label"]) for t in revenue["raw_numeric_tokens"]}
        self.assertEqual(
            tokens,
            {
                (100.00, 2025, "three-month"), (90.00, 2024, "three-month"),
                (250.00, 2025, "six-month"), (230.00, 2024, "six-month"),
            },
        )

    def test_legacy_mapped_year_values_field_unchanged_construction(self):
        # mapped_year_values (the pre-fix, legacy field) must still exist,
        # still be year-only-keyed, and still reflect the pre-fix
        # first-wins collapsing behavior exactly — proving this revision
        # did not remove or reshape it, only added mapped_period_year_values
        # alongside it.
        pdf_bytes = _build_period_block_fixture_pdf_bytes()
        result = extract_from_bytes(pdf_bytes, ticker="2020", fiscal_year=2025, period_type="Q2", fiscal_quarter=2)
        revenue = [c for c in result["candidates"] if c["concept"] == "revenue"][0]
        self.assertIn(2025, revenue["mapped_year_values"])
        self.assertIn(2024, revenue["mapped_year_values"])
        self.assertIsInstance(list(revenue["mapped_year_values"].keys())[0], int)


class TestPeriodBlockDisambiguationRealPDF(unittest.TestCase):
    """Real-bytes regression test against the actual SABIC Agri-Nutrients
    Q2 2026 filing obtained and diagnosed this session (see module-level
    REAL_QUARTERLY_FIXTURE_PATH/REAL_QUARTERLY_FIXTURE_SHA256). Skips
    (does not fail) if the fixture file is not present in this checkout —
    it is a real, ~1.15 MB downloaded filing, not yet committed as of
    this revision (a separate, explicit decision — see this task's own
    final report)."""

    @classmethod
    def setUpClass(cls):
        if not REAL_QUARTERLY_FIXTURE_PATH.exists():
            raise unittest.SkipTest(
                f"real PDF fixture not present at {REAL_QUARTERLY_FIXTURE_PATH} — "
                "this test requires the actual downloaded SABIC Agri-Nutrients Q2 2026 "
                "filing (SHA-256 " + REAL_QUARTERLY_FIXTURE_SHA256 + "); skipping rather "
                "than failing, since whether to commit this real binary into the repo "
                "is a separate, not-yet-made decision."
            )
        cls.pdf_bytes = REAL_QUARTERLY_FIXTURE_PATH.read_bytes()

    def test_fixture_integrity_sha256(self):
        self.assertEqual(hashlib.sha256(self.pdf_bytes).hexdigest(), REAL_QUARTERLY_FIXTURE_SHA256)

    def test_q2_selects_three_month_gross_profit(self):
        result = extract_from_bytes(self.pdf_bytes, ticker="2020", fiscal_year=2026, period_type="Q2", fiscal_quarter=2)
        gp = [c for c in result["candidates"] if c["concept"] == "gross_profit"][0]
        self.assertTrue(gp["year_resolved"])
        self.assertEqual(gp["requested_year_value"], 712192.0)  # real, printed Q2 2026 gross profit

    def test_h1_selects_six_month_gross_profit(self):
        result = extract_from_bytes(self.pdf_bytes, ticker="2020", fiscal_year=2026, period_type="H1")
        gp = [c for c in result["candidates"] if c["concept"] == "gross_profit"][0]
        self.assertTrue(gp["year_resolved"])
        self.assertEqual(gp["requested_year_value"], 2080959.0)  # real, printed H1 2026 (YTD) gross profit

    def test_fy_request_against_this_ambiguous_real_document_does_not_guess(self):
        result = extract_from_bytes(self.pdf_bytes, ticker="2020", fiscal_year=2026, period_type="FY")
        gp = [c for c in result["candidates"] if c["concept"] == "gross_profit"][0]
        self.assertFalse(gp["year_resolved"])
        self.assertIsNone(gp["requested_year_value"])

    def test_comparative_year_2025_also_disambiguated_correctly(self):
        q2_result = extract_from_bytes(self.pdf_bytes, ticker="2020", fiscal_year=2025, period_type="Q2", fiscal_quarter=2)
        h1_result = extract_from_bytes(self.pdf_bytes, ticker="2020", fiscal_year=2025, period_type="H1")
        q2_gp = [c for c in q2_result["candidates"] if c["concept"] == "gross_profit"][0]
        h1_gp = [c for c in h1_result["candidates"] if c["concept"] == "gross_profit"][0]
        self.assertEqual(q2_gp["requested_year_value"], 1266945.0)  # real, printed Q2 2025 comparative
        self.assertEqual(h1_gp["requested_year_value"], 2432338.0)  # real, printed H1 2025 comparative


if __name__ == "__main__":
    unittest.main()
