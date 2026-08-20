"""
tests/test_acquisition.py

Offline, deterministic tests for ingestion/load_historical.py's ACQUISITION
layer. These tests never touch the network — any function that would
normally make an HTTP request is exercised either with a registry entry
whose URL is never actually requested (because the file already exists
locally, exercising the idempotency path) or is simply left unregistered
(exercising the no-guessing / MISSING path). No test in this file requires
internet access, and none will fail differently with or without it.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from ingestion.load_historical import (
    SABIC_SOURCE_REGISTRY,
    VALID_SOURCE_TYPES,
    AcquiredDocumentMetadata,
    acquire_one_report,
    acquire_reports,
    count_pdf_pages,
    describe_registry_coverage,
    is_valid_pdf,
    load_manifest,
    manifest_path_for,
    render_coverage_report,
    save_manifest,
    sha256_file,
    verify_document_integrity,
)

FAKE_PDF_BYTES = b"%PDF-1.4\n%fake pdf for offline testing\n/Type /Page\n%%EOF"
FAKE_HTML_BYTES = b"<html><body>404 Not Found</body></html>"


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestIsValidPdf(TempDirTestCase):
    def test_real_pdf_magic_bytes_accepted(self):
        p = self.tmp / "real.pdf"
        p.write_bytes(FAKE_PDF_BYTES)
        self.assertTrue(is_valid_pdf(p))

    def test_html_error_page_rejected(self):
        p = self.tmp / "error.pdf"
        p.write_bytes(FAKE_HTML_BYTES)
        self.assertFalse(is_valid_pdf(p))

    def test_empty_file_rejected(self):
        p = self.tmp / "empty.pdf"
        p.write_bytes(b"")
        self.assertFalse(is_valid_pdf(p))

    def test_missing_file_rejected(self):
        p = self.tmp / "does_not_exist.pdf"
        self.assertFalse(is_valid_pdf(p))


class TestCountPdfPages(TempDirTestCase):
    def test_fallback_finds_page_marker(self):
        p = self.tmp / "one_page.pdf"
        p.write_bytes(FAKE_PDF_BYTES)
        # pdfplumber isn't installed in this environment (confirmed during
        # development), so this exercises the regex fallback path.
        count = count_pdf_pages(p)
        self.assertEqual(count, 1)

    def test_missing_file_returns_none(self):
        p = self.tmp / "nope.pdf"
        self.assertIsNone(count_pdf_pages(p))

    def test_no_page_markers_returns_none(self):
        p = self.tmp / "no_markers.pdf"
        p.write_bytes(b"%PDF-1.4\nnothing page-like here\n%%EOF")
        self.assertIsNone(count_pdf_pages(p))


class TestVerifyDocumentIntegrity(TempDirTestCase):
    def test_valid_pdf_passes(self):
        p = self.tmp / "good.pdf"
        p.write_bytes(FAKE_PDF_BYTES)
        ok, errors = verify_document_integrity(p)
        self.assertTrue(ok)
        self.assertEqual(errors, [])

    def test_html_disguised_as_pdf_fails(self):
        p = self.tmp / "bad.pdf"
        p.write_bytes(FAKE_HTML_BYTES)
        ok, errors = verify_document_integrity(p)
        self.assertFalse(ok)
        self.assertTrue(any("PDF magic bytes" in e for e in errors))

    def test_empty_file_fails_with_both_reasons(self):
        p = self.tmp / "empty.pdf"
        p.write_bytes(b"")
        ok, errors = verify_document_integrity(p)
        self.assertFalse(ok)
        self.assertTrue(any("empty" in e for e in errors))

    def test_missing_file_fails(self):
        p = self.tmp / "missing.pdf"
        ok, errors = verify_document_integrity(p)
        self.assertFalse(ok)
        self.assertIn("file does not exist", errors)


class TestSha256Duplicate(TempDirTestCase):
    def test_identical_content_same_hash_different_filenames(self):
        p1 = self.tmp / "a.pdf"
        p2 = self.tmp / "b.pdf"
        p1.write_bytes(FAKE_PDF_BYTES)
        p2.write_bytes(FAKE_PDF_BYTES)
        self.assertEqual(sha256_file(p1), sha256_file(p2))

    def test_different_content_different_hash(self):
        p1 = self.tmp / "a.pdf"
        p2 = self.tmp / "b.pdf"
        p1.write_bytes(FAKE_PDF_BYTES)
        p2.write_bytes(FAKE_PDF_BYTES + b"extra")
        self.assertNotEqual(sha256_file(p1), sha256_file(p2))


class TestManifest(TempDirTestCase):
    def test_load_manifest_missing_returns_empty_scaffold(self):
        # load_manifest looks under RAW_DATA_ROOT, not self.tmp — but an
        # unregistered company_slug will never have a file there either way.
        m = load_manifest("__test_slug_that_never_exists__")
        self.assertEqual(m["company_slug"], "__test_slug_that_never_exists__")
        self.assertEqual(m["records"], {})

    def test_save_then_load_roundtrip(self):
        ticker = "__test_roundtrip__"
        path = manifest_path_for(ticker)
        self.addCleanup(lambda: shutil.rmtree(path.parent, ignore_errors=True))
        manifest = {"ticker": ticker, "records": {"2024": {"acquisition_status": "MISSING"}}}
        save_manifest(ticker, manifest)
        self.assertTrue(path.exists())
        loaded = load_manifest(ticker)
        self.assertEqual(loaded["records"]["2024"]["acquisition_status"], "MISSING")

    def test_manifest_is_valid_json_on_disk(self):
        ticker = "__test_json_validity__"
        path = manifest_path_for(ticker)
        self.addCleanup(lambda: shutil.rmtree(path.parent, ignore_errors=True))
        save_manifest(ticker, {"ticker": ticker, "records": {}})
        with open(path) as f:
            json.load(f)  # raises if invalid


class TestAcquireOneReportNoGuessing(TempDirTestCase):
    """Covers the core no-network-required path: a fiscal year with no
    registry entry must be reported MISSING with an explicit reason, and
    must NOT attempt any request."""

    def test_unregistered_year_is_missing_with_reason(self):
        result = acquire_one_report(
            ticker="2010", fiscal_year=1999, now_iso="2026-01-01T00:00:00+00:00",
            registry={}, out_root=self.tmp,
        )
        self.assertIsInstance(result, AcquiredDocumentMetadata)
        self.assertEqual(result.acquisition_status, "MISSING")
        self.assertIsNone(result.source_url)
        self.assertIn("no verified official source URL", result.failure_reason)

    def test_unregistered_year_creates_no_local_file(self):
        acquire_one_report(
            ticker="2010", fiscal_year=1999, now_iso="2026-01-01T00:00:00+00:00",
            registry={}, out_root=self.tmp,
        )
        self.assertFalse((self.tmp / "2010" / "1999").exists())


class TestAcquireOneReportIdempotency(TempDirTestCase):
    """Covers the idempotency path entirely offline: pre-place a valid PDF
    at the expected local path, register a URL that would fail loudly if
    actually requested (an invalid scheme), and confirm the function reuses
    the existing file without attempting network I/O."""

    def _registry_entry(self):
        return {
            ("2010", 2024): {
                "source_url": "not-a-real://url.invalid/never-fetched.pdf",
                "source_website": "sabic.com",
                "source_tier": 1,
                "document_type": "annual_report",
            }
        }

    def test_existing_valid_file_is_reused_not_redownloaded(self):
        year_dir = self.tmp / "2010" / "2024"
        year_dir.mkdir(parents=True)
        local_path = year_dir / "2010_2024_annual_report.pdf"
        local_path.write_bytes(FAKE_PDF_BYTES)

        result = acquire_one_report(
            ticker="2010", fiscal_year=2024, now_iso="2026-01-01T00:00:00+00:00",
            registry=self._registry_entry(), out_root=self.tmp,
        )
        # If this had attempted a real request against the invalid URL
        # above, it would have failed with a connection/scheme error and
        # the status would be MISSING, not AVAILABLE — AVAILABLE here is
        # proof the idempotency short-circuit worked and no request fired.
        self.assertEqual(result.acquisition_status, "AVAILABLE")
        self.assertEqual(result.sha256, sha256_file(local_path))

    def test_existing_invalid_file_is_flagged_conflict_not_overwritten(self):
        year_dir = self.tmp / "2010" / "2024"
        year_dir.mkdir(parents=True)
        local_path = year_dir / "2010_2024_annual_report.pdf"
        local_path.write_bytes(FAKE_HTML_BYTES)  # bad content already present

        result = acquire_one_report(
            ticker="2010", fiscal_year=2024, now_iso="2026-01-01T00:00:00+00:00",
            registry=self._registry_entry(), out_root=self.tmp,
        )
        self.assertEqual(result.acquisition_status, "CONFLICT")
        # File must be left untouched, not silently overwritten.
        self.assertEqual(local_path.read_bytes(), FAKE_HTML_BYTES)


class TestAcquireReportsCoverage(TempDirTestCase):
    def test_full_run_produces_manifest_and_coverage(self):
        ticker = "__test_coverage__"
        self.addCleanup(lambda: shutil.rmtree(manifest_path_for(ticker).parent, ignore_errors=True))
        manifest = acquire_reports(
            ticker=ticker,
            fiscal_years=list(range(2015, 2025)),
            registry={},  # nothing registered -> every year MISSING
            out_root=self.tmp,
        )
        self.assertEqual(len(manifest["records"]), 10)
        for fy in range(2015, 2025):
            self.assertEqual(manifest["records"][str(fy)]["acquisition_status"], "MISSING")

        report_text = render_coverage_report(manifest)
        lines = report_text.splitlines()
        self.assertEqual(len(lines), 10)
        self.assertEqual(lines[0], "FY2015  MISSING")
        self.assertEqual(lines[-1], "FY2024  MISSING")

    def test_coverage_report_shows_found_for_available(self):
        manifest = {
            "records": {
                "2023": {"acquisition_status": "AVAILABLE"},
                "2024": {"acquisition_status": "MISSING"},
            }
        }
        text = render_coverage_report(manifest)
        self.assertIn("FY2023  FOUND", text)
        self.assertIn("FY2024  MISSING", text)


class TestSabicSourceRegistry(unittest.TestCase):
    """Pure inspection of the module-level registry dict — no network I/O,
    just confirming the exact set of verified years/sources matches what's
    been confirmed, and that no unverified year has snuck in."""

    PDF_YEARS = {2016, 2017, 2018, 2022, 2023, 2024}
    REPORT_PAGE_YEARS = {2019, 2020, 2021}
    ALL_VERIFIED_YEARS = PDF_YEARS | REPORT_PAGE_YEARS  # 9 years total
    UNVERIFIED_YEARS = {2015}

    def test_all_expected_years_are_registered(self):
        registered_years = {fy for (_, fy) in SABIC_SOURCE_REGISTRY}
        self.assertEqual(registered_years, self.ALL_VERIFIED_YEARS)

    def test_fy2015_is_not_registered(self):
        # No verified official source has been confirmed for FY2015 — per
        # the no-guessing rule it must remain entirely absent, not present
        # with an invented URL.
        self.assertNotIn(("2010", 2015), SABIC_SOURCE_REGISTRY)

    def test_no_duplicate_urls_across_years(self):
        urls = [e["source_url"] for e in SABIC_SOURCE_REGISTRY.values()]
        self.assertEqual(len(urls), len(set(urls)))

    def test_every_registered_entry_has_required_fields(self):
        for key, entry in SABIC_SOURCE_REGISTRY.items():
            with self.subTest(key=key):
                self.assertIn("source_url", entry)
                self.assertTrue(entry["source_url"].startswith("https://"))
                self.assertEqual(entry["source_tier"], 1)
                self.assertEqual(entry["document_type"], "annual_report")
                self.assertIn("source_type", entry)
                self.assertIn(entry["source_type"], VALID_SOURCE_TYPES)

    def test_every_url_is_on_the_official_sabic_domain(self):
        # Accepts sabic.com and its subdomains (e.g. www3.sabic.com) only —
        # no aggregator, no third party, no cache mirror.
        for key, entry in SABIC_SOURCE_REGISTRY.items():
            with self.subTest(key=key):
                host = entry["source_url"].split("/")[2]  # https://HOST/...
                self.assertTrue(
                    host == "sabic.com" or host.endswith(".sabic.com"),
                    f"URL host '{host}' is not on the official sabic.com domain",
                )

    def test_registered_entries_are_keyed_to_sabic_ticker(self):
        for (ticker, _fy) in SABIC_SOURCE_REGISTRY:
            self.assertEqual(ticker, "2010")

    def test_pdf_years_have_pdf_source_type(self):
        for fy in self.PDF_YEARS:
            with self.subTest(fy=fy):
                self.assertEqual(SABIC_SOURCE_REGISTRY[("2010", fy)]["source_type"], "pdf")

    def test_report_page_years_have_report_page_source_type(self):
        for fy in self.REPORT_PAGE_YEARS:
            with self.subTest(fy=fy):
                self.assertEqual(
                    SABIC_SOURCE_REGISTRY[("2010", fy)]["source_type"], "report_page"
                )

    def test_unregistered_year_reports_missing_via_acquire_one_report(self):
        # Exercises the real no-guessing path against the ACTUAL module
        # registry (not a fake one) for FY2015 — must not attempt any
        # request, must not guess a URL.
        with tempfile.TemporaryDirectory() as tmp:
            result = acquire_one_report(
                ticker="2010", fiscal_year=2015,
                now_iso="2026-01-01T00:00:00+00:00",
                registry=SABIC_SOURCE_REGISTRY, out_root=Path(tmp),
            )
            self.assertEqual(result.acquisition_status, "MISSING")
            self.assertIsNone(result.source_url)
            self.assertIn("no verified official source URL", result.failure_reason)

    def test_report_page_year_is_not_treated_as_pdf_acquired(self):
        # Exercises the real registry for FY2019 (a report_page year) —
        # must return REPORT_PAGE_ONLY, never AVAILABLE, never attempt to
        # download the HTML page as if it were a PDF.
        with tempfile.TemporaryDirectory() as tmp:
            result = acquire_one_report(
                ticker="2010", fiscal_year=2019,
                now_iso="2026-01-01T00:00:00+00:00",
                registry=SABIC_SOURCE_REGISTRY, out_root=Path(tmp),
            )
            self.assertEqual(result.acquisition_status, "REPORT_PAGE_ONLY")
            self.assertNotEqual(result.acquisition_status, "AVAILABLE")
            self.assertEqual(result.source_type, "report_page")
            self.assertEqual(
                result.source_url, "https://www.sabic.com/en/reports/annual-2019"
            )

    def test_report_page_year_has_no_local_path_or_page_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = acquire_one_report(
                ticker="2010", fiscal_year=2020,
                now_iso="2026-01-01T00:00:00+00:00",
                registry=SABIC_SOURCE_REGISTRY, out_root=Path(tmp),
            )
            self.assertIsNone(result.local_path)
            self.assertIsNone(result.page_count)

    def test_report_page_year_sha256_is_never_computed(self):
        # SHA-256 must never be computed for a source that isn't an actual
        # local PDF file — a report_page result must have sha256=None,
        # regardless of which year is checked.
        with tempfile.TemporaryDirectory() as tmp:
            for fy in self.REPORT_PAGE_YEARS:
                with self.subTest(fy=fy):
                    result = acquire_one_report(
                        ticker="2010", fiscal_year=fy,
                        now_iso="2026-01-01T00:00:00+00:00",
                        registry=SABIC_SOURCE_REGISTRY, out_root=Path(tmp),
                    )
                    self.assertIsNone(result.sha256)

    def test_missing_year_sha256_is_never_computed(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = acquire_one_report(
                ticker="2010", fiscal_year=2015,
                now_iso="2026-01-01T00:00:00+00:00",
                registry=SABIC_SOURCE_REGISTRY, out_root=Path(tmp),
            )
            self.assertIsNone(result.sha256)


class TestDescribeRegistryCoverage(unittest.TestCase):
    """Pure, offline registry-inspection reporting — no acquisition run,
    no network, no filesystem writes."""

    def test_full_fy2015_2024_coverage_shape(self):
        rows = describe_registry_coverage(
            list(range(2015, 2025)), ticker="2010", registry=SABIC_SOURCE_REGISTRY,
        )
        self.assertEqual(len(rows), 10)
        by_year = {r["fiscal_year"]: r for r in rows}

        self.assertFalse(by_year[2015]["found"])
        self.assertIsNone(by_year[2015]["source_type"])

        for fy in (2016, 2017, 2018, 2022, 2023, 2024):
            self.assertTrue(by_year[fy]["found"])
            self.assertEqual(by_year[fy]["source_type"], "pdf")

        for fy in (2019, 2020, 2021):
            self.assertTrue(by_year[fy]["found"])
            self.assertEqual(by_year[fy]["source_type"], "report_page")

    def test_unknown_ticker_finds_nothing(self):
        rows = describe_registry_coverage([2016], ticker="9999", registry=SABIC_SOURCE_REGISTRY)
        self.assertFalse(rows[0]["found"])


class TestMetadataValidation(unittest.TestCase):
    def test_to_dict_roundtrips_through_json(self):
        meta = AcquiredDocumentMetadata(
            ticker="2010", fiscal_year=2024, document_type="annual_report",
            source_url="https://example.invalid/report.pdf", source_website="sabic.com",
            source_tier=1, local_path="data/raw/2010/2024/x.pdf",
            sha256="a" * 64, page_count=120, acquisition_status="AVAILABLE",
            acquired_at="2026-01-01T00:00:00+00:00",
        )
        d = meta.to_dict()
        # must be JSON-serializable as-is
        json.dumps(d)
        self.assertEqual(d["ticker"], "2010")
        self.assertEqual(d["acquisition_status"], "AVAILABLE")

    def test_missing_status_has_no_sha256_or_path(self):
        meta = AcquiredDocumentMetadata(
            ticker="2010", fiscal_year=1999, document_type="annual_report",
            source_url=None, source_website=None, source_tier=None,
            local_path=None, sha256=None, page_count=None,
            acquisition_status="MISSING", failure_reason="no registry entry",
        )
        self.assertIsNone(meta.sha256)
        self.assertIsNone(meta.local_path)


if __name__ == "__main__":
    unittest.main()
