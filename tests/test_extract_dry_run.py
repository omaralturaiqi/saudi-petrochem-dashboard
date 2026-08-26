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
    PdfBytesInvalid,
    extract_from_bytes,
    verify_pdf_bytes,
)
from scripts.dry_run_extract import build_arg_parser, run as script_run

REPO_ROOT = Path(__file__).resolve().parent.parent
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
    def test_multiple_numeric_columns_produce_ambiguity_warning(self):
        # (F) a line with two plausible numeric columns must produce an
        # explicit ambiguity warning, and BOTH values must be preserved
        # (never guessed/dropped).
        pdf_bytes = _build_fixture_pdf_bytes()
        result = extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024)
        revenue_candidates = [c for c in result["candidates"] if c["concept"] == "revenue"]
        self.assertTrue(revenue_candidates, "fixture must produce a revenue candidate")
        for c in revenue_candidates:
            self.assertIsNotNone(c["value_col1"])
            self.assertIsNotNone(c["value_col2"])
            self.assertTrue(any("not disambiguated" in w for w in c["warnings"]))


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


if __name__ == "__main__":
    unittest.main()
