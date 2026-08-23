"""
tests/test_acquire_official_pdfs_script.py

Offline tests for scripts/acquire_official_pdfs.py's pure logic (target
counting, summary printing). Does NOT test run_connectivity_smoke_test() or
run_full_acquisition() directly, since both perform real network I/O by
design — those are exercised manually/in a real network-enabled
environment, never in this offline suite.

The actual download/integrity/idempotency behavior these commands rely on
(acquire_one_report, verify_document_integrity, sha256_file, etc.) is
already covered by tests/test_acquisition.py and
tests/test_acquisition_generic.py — not duplicated here.
"""
import io
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import requests

from scripts.acquire_official_pdfs import (
    count_pdf_targets,
    print_summary,
    _summarize_response,
    _timed_get,
    diagnose_http_investigation,
    CONTROL_TEST_US_PDF_URL,
    DIAGNOSTIC_REPORT_PAGE_CANDIDATES,
)


def _fake_response(status_code=200, content=b"", content_type="application/pdf",
                    url="https://example.invalid/final", history=None, headers=None):
    """Builds a real requests.Response object (not a live one) so
    _summarize_response() is exercised against the exact type it receives
    in production, with zero network I/O."""
    resp = requests.Response()
    resp.status_code = status_code
    resp._content = content
    resp.url = url
    resp.history = history or []
    resp.headers = requests.structures.CaseInsensitiveDict(headers or {})
    resp.headers.setdefault("Content-Type", content_type)
    resp.encoding = "utf-8"
    return resp


class TestCountPdfTargets(unittest.TestCase):
    def test_matches_known_registry_discovery_total(self):
        # As of the last Registry Discovery pass, exactly 29 source_type
        # == "pdf" entries exist across all companies. If this drifts,
        # that's a real registry change that should be visible here.
        self.assertEqual(count_pdf_targets(), 29)

    def test_returns_an_int_not_negative(self):
        count = count_pdf_targets()
        self.assertIsInstance(count, int)
        self.assertGreaterEqual(count, 0)


class TestPrintSummary(unittest.TestCase):
    def _fake_manifest(self, records):
        return {"records": records}

    def test_counts_available_failed_unavailable_correctly(self):
        manifests = {
            "companyA": self._fake_manifest({
                "2020": {"source_type": "pdf", "acquisition_status": "AVAILABLE"},
                "2021": {"source_type": "pdf", "acquisition_status": "FAILED"},
            }),
            "companyB": self._fake_manifest({
                "2022": {"source_type": "pdf", "acquisition_status": "SOURCE_DISCOVERED_BUT_FILE_UNAVAILABLE"},
                "2023": {"source_type": "report_page", "acquisition_status": "REPORT_PAGE_ONLY"},
                "2024": {"source_type": None, "acquisition_status": "MISSING"},
            }),
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_summary(manifests)
        output = buf.getvalue()
        self.assertIn("Downloaded successfully (AVAILABLE) = 1", output)
        self.assertIn("Failed                 = 1", output)
        self.assertIn("Unavailable            = 1", output)

    def test_non_pdf_entries_are_excluded_from_counts(self):
        manifests = {
            "companyA": self._fake_manifest({
                "2020": {"source_type": "report_page", "acquisition_status": "REPORT_PAGE_ONLY"},
                "2021": {"source_type": None, "acquisition_status": "MISSING"},
            }),
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_summary(manifests)
        output = buf.getvalue()
        self.assertIn("Downloaded successfully (AVAILABLE) = 0", output)
        self.assertIn("Failed                 = 0", output)
        self.assertIn("Unavailable            = 0", output)

    def test_empty_manifests_produce_zero_counts(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_summary({})
        output = buf.getvalue()
        self.assertIn("Downloaded successfully (AVAILABLE) = 0", output)


class TestSummarizeResponsePdf(unittest.TestCase):
    def test_pdf_response_has_no_html_fields(self):
        resp = _fake_response(status_code=200, content=b"%PDF-1.4 rest of pdf bytes",
                               content_type="application/pdf")
        buf = io.StringIO()
        with redirect_stdout(buf):
            summary = _summarize_response("test", resp)
        self.assertEqual(summary["http_status"], 200)
        self.assertTrue(summary["starts_with_pdf_magic"])
        self.assertNotIn("html_title", summary)
        self.assertNotIn("html_snippet_first_500_chars", summary)

    def test_redirect_history_is_captured(self):
        redirect_hop = _fake_response(status_code=301, url="https://example.invalid/old")
        resp = _fake_response(status_code=200, content=b"%PDF-...", history=[redirect_hop])
        buf = io.StringIO()
        with redirect_stdout(buf):
            summary = _summarize_response("test", resp)
        self.assertEqual(summary["redirect_history"], [(301, "https://example.invalid/old")])


class TestSummarizeResponseHtml(unittest.TestCase):
    def test_html_response_extracts_title_and_snippet(self):
        html = b"<html><head><title>Internal Server Error</title></head><body>500 error occurred</body></html>"
        resp = _fake_response(status_code=500, content=html, content_type="text/html; charset=utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            summary = _summarize_response("test", resp)
        self.assertFalse(summary["starts_with_pdf_magic"])
        self.assertEqual(summary["html_title"], "Internal Server Error")
        self.assertIn("500", summary["waf_signal_keywords_in_body"])
        self.assertIn("error", summary["waf_signal_keywords_in_body"])
        self.assertIn("500 error occurred", summary["html_snippet_first_500_chars"])

    def test_waf_headers_are_picked_up_when_present(self):
        html = b"<html><head><title>Attention Required</title></head><body>cloudflare check</body></html>"
        resp = _fake_response(status_code=403, content=html, content_type="text/html",
                               headers={"CF-RAY": "abc123-FRA", "Server": "cloudflare"})
        buf = io.StringIO()
        with redirect_stdout(buf):
            summary = _summarize_response("test", resp)
        self.assertEqual(summary["waf_signal_headers"].get("CF-RAY"), "abc123-FRA")
        self.assertIn("cloudflare", summary["waf_signal_keywords_in_body"])

    def test_no_waf_signals_present_reports_empty(self):
        html = b"<html><head><title>Plain Page</title></head><body>nothing unusual here</body></html>"
        resp = _fake_response(status_code=200, content=html, content_type="text/html")
        buf = io.StringIO()
        with redirect_stdout(buf):
            summary = _summarize_response("test", resp)
        self.assertEqual(summary["waf_signal_headers"], {})
        self.assertEqual(summary["waf_signal_keywords_in_body"], [])


class TestDiagnoseHttpInvestigationNoRegistryEntry(unittest.TestCase):
    def test_missing_registry_entry_returns_immediately_without_network(self):
        # sabic ticker "2010" has no registry entry for FY1999 — this must
        # short-circuit before any requests.get() is attempted, exactly
        # like diagnose_single()'s equivalent guard.
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = diagnose_http_investigation("sabic", 1999)
        self.assertEqual(result.get("error"), "no registry entry")
        self.assertNotIn("test_1_default_headers", result)
        self.assertNotIn("control_test_us_pdf", result)


class TestTimedGet(unittest.TestCase):
    def test_successful_call_returns_response_elapsed_and_no_error(self):
        fake = _fake_response(status_code=200, content=b"%PDF-...")
        with patch("requests.get", return_value=fake) as mock_get:
            resp, elapsed, err = _timed_get("https://example.invalid/x", timeout=30)
        self.assertIs(resp, fake)
        self.assertIsNone(err)
        self.assertGreaterEqual(elapsed, 0)
        mock_get.assert_called_once_with("https://example.invalid/x", timeout=30)

    def test_exception_returns_none_response_and_error_string(self):
        with patch("requests.get", side_effect=requests.exceptions.ConnectionError("boom")):
            resp, elapsed, err = _timed_get("https://example.invalid/x", timeout=30)
        self.assertIsNone(resp)
        self.assertIn("ConnectionError", err)
        self.assertGreaterEqual(elapsed, 0)


class TestDiagnoseHttpInvestigationControlTest(unittest.TestCase):
    """Full flow with requests.get mocked (no real network I/O) to verify
    the CONTROL TEST — U.S. PDF section runs alongside the two existing
    SABIC sections, in the required output order, without touching any
    registry."""

    def _sabic_pdf_url(self):
        from ingestion.load_historical import COMPANY_SOURCE_REGISTRIES, CONFIRMED_COMPANY_TICKERS
        ticker = CONFIRMED_COMPANY_TICKERS["sabic"]
        return COMPANY_SOURCE_REGISTRIES["sabic"][(ticker, 2024)]["source_url"]

    def test_three_named_sections_and_result_keys_present(self):
        sabic_pdf_url = self._sabic_pdf_url()
        report_page_url = DIAGNOSTIC_REPORT_PAGE_CANDIDATES["sabic"]

        def fake_get(url, **kwargs):
            if url == CONTROL_TEST_US_PDF_URL:
                return _fake_response(status_code=200, content=b"%PDF-1.7 control pdf bytes",
                                       content_type="application/pdf", url=url)
            if url == sabic_pdf_url:
                html = b"<html><head><title>Internal Server Error</title></head><body>500</body></html>"
                return _fake_response(status_code=500, content=html, content_type="text/html", url=url)
            if url == report_page_url:
                html = b"<html><head><title>Annual Report</title></head><body>ok</body></html>"
                return _fake_response(status_code=200, content=html, content_type="text/html", url=url)
            raise AssertionError(f"unexpected URL requested in test: {url}")

        buf = io.StringIO()
        with patch("requests.get", side_effect=fake_get):
            with redirect_stdout(buf):
                result = diagnose_http_investigation("sabic", 2024)
        output = buf.getvalue()

        self.assertIn("CONTROL TEST — U.S. PDF", output)
        self.assertIn("SABIC FY2024 PDF", output)
        self.assertIn("SABIC FY2024 official report/index page", output)
        # Control section must appear before the SABIC PDF section in output.
        self.assertLess(output.index("CONTROL TEST — U.S. PDF"), output.index("SABIC FY2024 PDF"))

        self.assertTrue(result["control_test_us_pdf"]["starts_with_pdf_magic"])
        self.assertEqual(result["control_test_us_pdf"]["http_status"], 200)
        self.assertEqual(result["test_1_default_headers"]["http_status"], 500)
        self.assertEqual(result["test_2_browser_headers"]["http_status"], 500)
        self.assertEqual(result["test_3_report_page"]["http_status"], 200)
        self.assertIn("elapsed_seconds", result["control_test_us_pdf"])

    def test_control_test_network_failure_does_not_crash_remaining_sections(self):
        sabic_pdf_url = self._sabic_pdf_url()
        report_page_url = DIAGNOSTIC_REPORT_PAGE_CANDIDATES["sabic"]

        def fake_get(url, **kwargs):
            if url == CONTROL_TEST_US_PDF_URL:
                raise requests.exceptions.ConnectionError("control host unreachable")
            if url == sabic_pdf_url:
                return _fake_response(status_code=200, content=b"%PDF-1.7 real sabic pdf", url=url)
            if url == report_page_url:
                return _fake_response(status_code=200, content=b"<html></html>", content_type="text/html", url=url)
            raise AssertionError(f"unexpected URL requested in test: {url}")

        buf = io.StringIO()
        with patch("requests.get", side_effect=fake_get):
            with redirect_stdout(buf):
                result = diagnose_http_investigation("sabic", 2024)
        self.assertIn("ConnectionError", result["control_test_us_pdf"]["error"])
        self.assertTrue(result["test_1_default_headers"]["starts_with_pdf_magic"])


class TestScriptDoesNotImportNetworkAtModuleLevel(unittest.TestCase):
    def test_module_imports_without_making_any_request(self):
        # Simply importing the module must not perform network I/O — this
        # is what allows this whole test file to run offline. If the
        # import above (at module load time of this test file) succeeded
        # without hanging or raising a network error, this invariant holds.
        import scripts.acquire_official_pdfs as mod
        self.assertTrue(hasattr(mod, "run_connectivity_smoke_test"))
        self.assertTrue(hasattr(mod, "run_full_acquisition"))


if __name__ == "__main__":
    unittest.main()
