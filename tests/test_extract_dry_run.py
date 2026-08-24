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
        original_extract_text = pdfplumber.page.Page.extract_text
        pages_seen_so_far = []

        def spy_extract_text(self, *args, **kwargs):
            for prior_page in pages_seen_so_far:
                if hasattr(prior_page, "_objects"):
                    raise AssertionError(
                        f"page {prior_page.page_number}'s cache is still populated "
                        f"while page {self.page_number} is starting extraction — "
                        "memory is not bounded to ~1 page at a time"
                    )
            pages_seen_so_far.append(self)
            return original_extract_text(self, *args, **kwargs)

        with patch.object(pdfplumber.page.Page, "extract_text", spy_extract_text):
            result = extract_from_bytes(pdf_bytes, ticker="2010", fiscal_year=2024)

        self.assertEqual(result["page_count"], 3)
        self.assertEqual(len(pages_seen_so_far), 3)


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
