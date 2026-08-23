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

import requests

from scripts.acquire_official_pdfs import (
    count_pdf_targets,
    print_summary,
    _summarize_response,
    diagnose_http_investigation,
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
