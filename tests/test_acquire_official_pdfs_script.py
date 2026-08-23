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
import json
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from urllib.parse import urljoin, urlparse

import requests

from scripts.acquire_official_pdfs import (
    count_pdf_targets,
    print_summary,
    _summarize_response,
    _timed_get,
    diagnose_http_investigation,
    CONTROL_TEST_US_PDF_URL,
    DIAGNOSTIC_REPORT_PAGE_CANDIDATES,
    extract_report_link_candidates,
    _classify_candidate,
    diagnose_report_page_links,
    _extract_js_endpoint_candidates,
    _parse_json_doc_blob,
    _classify_endpoint_candidate,
    _looks_like_document_url,
    diagnose_report_endpoints,
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


class TestExtractReportLinkCandidates(unittest.TestCase):
    BASE_URL = "https://www.sabic.com/en/investors/performance-financial-highlights/annual-report"

    def test_finds_direct_href_and_resolves_relative_url(self):
        html = '''
        <html><body>
          <p>SABIC Integrated Annual Report 2024</p>
          <a href="/en/Images/SABIC-Annual-Report-2024-EN.pdf">Download FY2024 report</a>
        </body></html>
        '''
        candidates = extract_report_link_candidates(html, self.BASE_URL)
        hrefs = [c for c in candidates if c["source"] == "a_href"]
        self.assertEqual(len(hrefs), 1)
        self.assertEqual(hrefs[0]["url"], "/en/Images/SABIC-Annual-Report-2024-EN.pdf")
        self.assertEqual(
            hrefs[0]["resolved_url"],
            "https://www.sabic.com/en/Images/SABIC-Annual-Report-2024-EN.pdf",
        )
        self.assertIn("2024", hrefs[0]["context"])

    def test_finds_iframe_embed_object_sources(self):
        html = '''
        <iframe src="/reports/2024-viewer.html"></iframe>
        <embed src="/reports/2024-embed.pdf">
        <object data="/reports/2024-object.pdf"></object>
        '''
        candidates = extract_report_link_candidates(html, self.BASE_URL)
        sources = {c["source"]: c["url"] for c in candidates}
        self.assertEqual(sources.get("iframe_src"), "/reports/2024-viewer.html")
        self.assertEqual(sources.get("embed_src"), "/reports/2024-embed.pdf")
        self.assertEqual(sources.get("object_data"), "/reports/2024-object.pdf")

    def test_finds_js_variable_and_fetch_reference(self):
        html = '''
        <script>
          var reportUrl = "https://cdn.sabic.com/2024/annual-report.pdf";
          fetch("/api/annual-reports/2024");
        </script>
        '''
        candidates = extract_report_link_candidates(html, self.BASE_URL)
        js = [c for c in candidates if c["source"] == "js_reference"]
        urls = {c["url"] for c in js}
        self.assertIn("https://cdn.sabic.com/2024/annual-report.pdf", urls)
        self.assertIn("/api/annual-reports/2024", urls)

    def test_finds_data_attribute_reference(self):
        html = '<div data-download-url="/files/2024-report-document.pdf"></div>'
        candidates = extract_report_link_candidates(html, self.BASE_URL)
        data_attrs = [c for c in candidates if c["source"] == "data_attribute"]
        self.assertEqual(len(data_attrs), 1)
        self.assertEqual(data_attrs[0]["url"], "/files/2024-report-document.pdf")

    def test_no_candidates_in_plain_html(self):
        html = "<html><body><p>Nothing to see here.</p></body></html>"
        candidates = extract_report_link_candidates(html, self.BASE_URL)
        self.assertEqual(candidates, [])


class TestClassifyCandidate(unittest.TestCase):
    def test_pdf_url_with_matching_year_is_verified(self):
        verdict = _classify_candidate(
            "/en/Images/SABIC-Annual-Report-2024-EN.pdf",
            "Download FY2024 report", 2024,
        )
        self.assertEqual(verdict, "VERIFIED_FY_CANDIDATE")

    def test_document_keyword_without_year_is_possible(self):
        verdict = _classify_candidate("/reports/download-report.pdf", "click here", 2024)
        self.assertEqual(verdict, "POSSIBLE_CANDIDATE")

    def test_year_present_but_no_document_keyword_is_unrelated(self):
        # "2024" appears but nothing marks it as a document/report link at
        # all -> not promoted to VERIFIED or POSSIBLE just for having a year.
        verdict = _classify_candidate("/careers/2024-jobs", "Careers page 2024", 2024)
        self.assertEqual(verdict, "UNRELATED")

    def test_older_year_report_link_is_unrelated_to_target_year(self):
        verdict = _classify_candidate(
            "/en/Images/SABIC-Annual-Report-2022-EN.pdf",
            "Download 2022 report", 2024,
        )
        # Has doc keywords ("annual"/"report"/"pdf") but not the 2024 target
        # year -> POSSIBLE, not VERIFIED (still surfaced, not silently lost).
        self.assertEqual(verdict, "POSSIBLE_CANDIDATE")

    def test_completely_unrelated_link_is_unrelated(self):
        verdict = _classify_candidate("/careers/apply", "Join our team", 2024)
        self.assertEqual(verdict, "UNRELATED")


class TestDiagnoseReportPageLinks(unittest.TestCase):
    def test_makes_exactly_one_request_and_classifies_sections(self):
        html = '''
        <html><body>
          <p>SABIC Integrated Annual Report 2024</p>
          <a href="/en/Images/SABIC-Annual-Report-2024-EN.pdf">Download FY2024</a>
          <a href="/en/Images/SABIC-Annual-Report-2022-EN.pdf">Download 2022 report</a>
          <a href="/careers/apply">Careers</a>
        </body></html>
        '''
        fake = _fake_response(status_code=200, content=html.encode("utf-8"),
                               content_type="text/html", url=DIAGNOSTIC_REPORT_PAGE_CANDIDATES["sabic"])
        buf = io.StringIO()
        with patch("requests.get", return_value=fake) as mock_get:
            with redirect_stdout(buf):
                result = diagnose_report_page_links("sabic", 2024)
        output = buf.getvalue()

        mock_get.assert_called_once()
        self.assertEqual(result["http_status"], 200)
        self.assertIn("VERIFIED FY2024 candidate", output)
        self.assertIn("POSSIBLE candidate", output)
        self.assertIn("UNRELATED report link", output)

        classifications = {c["url"]: c["classification"] for c in result["candidates"]}
        self.assertEqual(
            classifications["/en/Images/SABIC-Annual-Report-2024-EN.pdf"],
            "VERIFIED_FY_CANDIDATE",
        )
        self.assertEqual(classifications["/careers/apply"], "UNRELATED")

    def test_no_known_report_page_short_circuits_without_network(self):
        buf = io.StringIO()
        with patch("requests.get") as mock_get:
            with redirect_stdout(buf):
                result = diagnose_report_page_links("yansab", 2024)
        mock_get.assert_not_called()
        self.assertEqual(result.get("error"), "no report page url")

    def test_non_html_response_produces_no_candidates(self):
        fake = _fake_response(status_code=200, content=b"%PDF-not actually html",
                               content_type="application/pdf")
        buf = io.StringIO()
        with patch("requests.get", return_value=fake):
            with redirect_stdout(buf):
                result = diagnose_report_page_links("sabic", 2024)
        self.assertEqual(result["candidates"], [])


class TestExtractJsEndpointCandidates(unittest.TestCase):
    def test_finds_fetch_call(self):
        js = 'fetch("/api/annual-reports/2024").then(r => r.json());'
        found = _extract_js_endpoint_candidates(js, "inline JS")
        calls = [c for c in found if "fetch()" in c["reason"]]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["raw_value"], "/api/annual-reports/2024")
        self.assertEqual(calls[0]["source"], "inline JS")

    def test_finds_axios_call(self):
        js = 'axios.get("/api/investor/documents").then(handleResponse);'
        found = _extract_js_endpoint_candidates(js, "external JS")
        calls = [c for c in found if "fetch()" in c["reason"]]
        self.assertEqual(calls[0]["raw_value"], "/api/investor/documents")

    def test_finds_xhr_open_call(self):
        js = 'var xhr = new XMLHttpRequest(); xhr.open("GET", "/api/reports/list");'
        found = _extract_js_endpoint_candidates(js, "inline JS")
        calls = [c for c in found if "fetch()" in c["reason"]]
        self.assertEqual(calls[0]["raw_value"], "/api/reports/list")

    def test_finds_generic_keyword_string_literal(self):
        js = 'const url = "/documents/publications/annual-report-2024.pdf";'
        found = _extract_js_endpoint_candidates(js, "inline JS")
        generic = [c for c in found if "string literal" in c["reason"]]
        self.assertTrue(any(c["raw_value"] == "/documents/publications/annual-report-2024.pdf"
                             for c in generic))

    def test_finds_valid_json_document_blob_with_year(self):
        js = '{"documentUrl": "/files/report-2024.pdf", "year": 2024}'
        found = _extract_js_endpoint_candidates(js, "inline JS")
        json_hits = [c for c in found if "JSON" in c["reason"]]
        self.assertEqual(len(json_hits), 1)
        self.assertEqual(json_hits[0]["raw_value"], "/files/report-2024.pdf")
        self.assertEqual(json_hits[0]["_json_year"], 2024)

    def test_no_candidates_in_unrelated_js(self):
        js = "function toggleMenu() { document.getElementById('nav').classList.toggle('open'); }"
        found = _extract_js_endpoint_candidates(js, "inline JS")
        self.assertEqual(found, [])


class TestParseJsonDocBlob(unittest.TestCase):
    def test_valid_json_with_file_url_and_year(self):
        blob = '{"fileUrl": "/x/report.pdf", "year": 2024}'
        entry = _parse_json_doc_blob(blob, "inline JS")
        self.assertEqual(entry["raw_value"], "/x/report.pdf")
        self.assertEqual(entry["_json_year"], 2024)

    def test_malformed_json_falls_back_to_regex(self):
        # Trailing comma makes this invalid strict JSON.
        blob = '{"pdfUrl": "/x/report-2024.pdf", "year": 2024,}'
        entry = _parse_json_doc_blob(blob, "inline JS")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["raw_value"], "/x/report-2024.pdf")
        self.assertEqual(entry["_json_year"], "2024")
        self.assertIn("regex fallback", entry["reason"])

    def test_no_document_field_returns_none(self):
        blob = '{"title": "Careers", "year": 2024}'
        self.assertIsNone(_parse_json_doc_blob(blob, "inline JS"))


class TestClassifyEndpointCandidate(unittest.TestCase):
    def test_api_path_with_keyword_is_high_confidence(self):
        verdict = _classify_endpoint_candidate("/api/annual-reports/2024", "fetch call", 2024)
        self.assertEqual(verdict, "HIGH_CONFIDENCE")

    def test_pdf_url_with_matching_year_is_high_confidence(self):
        verdict = _classify_endpoint_candidate("/files/report-2024.pdf", "", 2024)
        self.assertEqual(verdict, "HIGH_CONFIDENCE")

    def test_json_year_field_matching_target_is_high_confidence_even_without_year_in_url(self):
        verdict = _classify_endpoint_candidate("/files/report.pdf", "", 2024, json_year=2024)
        self.assertEqual(verdict, "HIGH_CONFIDENCE")

    def test_json_year_field_not_matching_target_is_not_high_confidence(self):
        verdict = _classify_endpoint_candidate("/files/report.pdf", "", 2024, json_year=2022)
        self.assertNotEqual(verdict, "HIGH_CONFIDENCE")

    def test_keyword_only_navigation_link_is_unrelated_not_possible(self):
        # This is the exact false-positive this fix addresses: a plain
        # navigation/index page (not a document, not an /api/ path) that
        # merely contains a report/investor keyword in its own path must
        # NOT be promoted to POSSIBLE just for that — SABIC's real page
        # has dozens of these (/en/investors, /en/reports, /en/newsandmedia
        # /reports, /en/sustainability/governance-and-reporting, ...) and
        # they used to drown out genuine document candidates.
        verdict = _classify_endpoint_candidate("/investor/publications", "some text", 2024)
        self.assertEqual(verdict, "UNRELATED")

    def test_api_path_without_any_keyword_is_possible_not_high(self):
        verdict = _classify_endpoint_candidate("/api/user/session", "", 2024)
        self.assertEqual(verdict, "POSSIBLE")

    def test_completely_unrelated_is_unrelated(self):
        verdict = _classify_endpoint_candidate("/careers/apply", "Join our team", 2024)
        self.assertEqual(verdict, "UNRELATED")

    def test_pdf_document_url_wrong_year_is_possible_not_unrelated(self):
        # The real SABIC case: a genuine, current annual-report PDF (the
        # 2025 report) is a real document — it must surface as POSSIBLE,
        # not get lost as UNRELATED just because the target year is 2024.
        verdict = _classify_endpoint_candidate(
            "https://www.sabic.com/en/Images/SABIC-Integrated-Annual-Report-2025-EN_tcm1010-49452.pdf",
            "Integrated Annual Report 2025 (PDF)", 2024,
        )
        self.assertEqual(verdict, "POSSIBLE")

    def test_sabic_images_path_without_pdf_extension_counts_as_document_url(self):
        verdict = _classify_endpoint_candidate(
            "https://www.sabic.com/en/Images/SABIC-Annual-Report-2024-EN", "", 2024,
        )
        self.assertEqual(verdict, "HIGH_CONFIDENCE")

    def test_document_url_with_query_string_is_still_detected(self):
        verdict = _classify_endpoint_candidate(
            "https://www.sabic.com/en/Images/SABIC-Annual-Report-2024-EN.pdf?v=2", "", 2024,
        )
        self.assertEqual(verdict, "HIGH_CONFIDENCE")


class TestLooksLikeDocumentUrl(unittest.TestCase):
    def test_pdf_extension_is_a_document(self):
        self.assertTrue(_looks_like_document_url("https://www.sabic.com/en/Images/report.pdf"))

    def test_pdf_extension_with_query_string_is_a_document(self):
        self.assertTrue(_looks_like_document_url("https://www.sabic.com/report.pdf?download=1"))

    def test_generic_js_bundle_is_not_a_document(self):
        self.assertFalse(_looks_like_document_url("https://www.sabic.com/dist/js/main.js?v=1"))

    def test_images_cdn_path_is_a_document(self):
        self.assertTrue(_looks_like_document_url("https://www.sabic.com/en/Images/SomeFile"))

    def test_plain_navigation_page_is_not_a_document(self):
        self.assertFalse(_looks_like_document_url("https://www.sabic.com/en/investors"))
        self.assertFalse(_looks_like_document_url("https://www.sabic.com/en/reports"))
        self.assertFalse(_looks_like_document_url("https://www.sabic.com/en/newsandmedia/reports"))


class TestDiagnoseReportEndpoints(unittest.TestCase):
    PAGE_URL = DIAGNOSTIC_REPORT_PAGE_CANDIDATES["sabic"]

    def test_no_known_report_page_short_circuits_without_network(self):
        buf = io.StringIO()
        with patch("requests.get") as mock_get:
            with redirect_stdout(buf):
                result = diagnose_report_endpoints("yansab", 2024)
        mock_get.assert_not_called()
        self.assertEqual(result.get("error"), "no report page url")

    def test_finds_high_confidence_json_endpoint_and_tests_it(self):
        html = f'''
        <html><body>
          <p>SABIC Integrated Annual Report 2024</p>
          <script>
            fetch("/api/annual-reports/2024").then(r => r.json());
          </script>
          <a href="/careers/apply">Careers</a>
        </body></html>
        '''
        api_url = urljoin(self.PAGE_URL, "/api/annual-reports/2024")
        endpoint_json = json.dumps({"documentUrl": "/files/SABIC-2024.pdf", "year": 2024}).encode()

        def fake_get(url, **kwargs):
            if url == self.PAGE_URL:
                return _fake_response(status_code=200, content=html.encode(), content_type="text/html", url=url)
            if url == api_url:
                return _fake_response(status_code=200, content=endpoint_json,
                                       content_type="application/json", url=url)
            raise AssertionError(f"unexpected URL requested: {url}")

        buf = io.StringIO()
        with patch("requests.get", side_effect=fake_get):
            with redirect_stdout(buf):
                result = diagnose_report_endpoints("sabic", 2024)
        output = buf.getvalue()

        self.assertIn("CANDIDATE ENDPOINT", output)
        self.assertIn("HIGH-CONFIDENCE REPORT ENDPOINT", output)
        self.assertIn("POSSIBLE REPORT ENDPOINT", output)
        self.assertIn("UNRELATED ENDPOINT", output)

        self.assertEqual(len(result["endpoint_test_results"]), 1)
        tested = result["endpoint_test_results"][0]
        self.assertTrue(tested["is_json"])
        self.assertEqual(tested["document_url_in_json"], "/files/SABIC-2024.pdf")
        self.assertIn("/files/SABIC-2024.pdf", output)

    def test_caps_endpoint_testing_at_three(self):
        html_parts = ["<html><body>"]
        api_urls = []
        for i in range(5):
            path = f"/api/annual-report-doc-{i}/2024"
            html_parts.append(f'<script>fetch("{path}");</script>')
            api_urls.append(urljoin(self.PAGE_URL, path))
        html_parts.append("</body></html>")
        html = "".join(html_parts)

        call_count = {"n": 0}

        def fake_get(url, **kwargs):
            if url == self.PAGE_URL:
                return _fake_response(status_code=200, content=html.encode(), content_type="text/html", url=url)
            call_count["n"] += 1
            return _fake_response(status_code=200, content=b"<html></html>", content_type="text/html", url=url)

        buf = io.StringIO()
        with patch("requests.get", side_effect=fake_get):
            with redirect_stdout(buf):
                result = diagnose_report_endpoints("sabic", 2024)
        output = buf.getvalue()
        self.assertEqual(call_count["n"], 3)
        self.assertEqual(len(result["endpoint_test_results"]), 3)
        self.assertIn("NOTE:", output)

    def test_keyword_relevant_cross_origin_js_is_still_fetched(self):
        # (C) Existing keyword-based relevant-JS behavior is unchanged: a
        # script on a DIFFERENT origin whose filename matches a report
        # keyword must still be fetched, exactly as before this change.
        html = '''
        <html><body>
          <script src="https://cdn.example.com/assets/annual-report-loader.js"></script>
        </body></html>
        '''
        relevant_js_url = "https://cdn.example.com/assets/annual-report-loader.js"
        js_content = b'fetch("/api/annual-reports/2024");'

        def fake_get(url, **kwargs):
            if url == self.PAGE_URL:
                return _fake_response(status_code=200, content=html.encode(), content_type="text/html", url=url)
            if url == relevant_js_url:
                return _fake_response(status_code=200, content=js_content,
                                       content_type="application/javascript", url=url)
            raise AssertionError(f"unexpected URL requested: {url}")

        buf = io.StringIO()
        with patch("requests.get", side_effect=fake_get):
            with redirect_stdout(buf):
                result = diagnose_report_endpoints("sabic", 2024)
        self.assertEqual(result["external_js_files_fetched"], 1)
        external_js_candidates = [c for c in result["candidates"] if c["source"] == "external JS"]
        self.assertTrue(any(c["raw_value"] == "/api/annual-reports/2024" for c in external_js_candidates))

    def test_cross_origin_js_without_keyword_is_excluded(self):
        # (B) A third-party/cross-origin script with a generic, non-keyword
        # filename must NOT be fetched — same-origin eligibility must never
        # be broadened to other domains.
        html = '''
        <html><body>
          <script src="https://cdn.example.com/dist/js/main.js"></script>
        </body></html>
        '''

        def fake_get(url, **kwargs):
            if url == self.PAGE_URL:
                return _fake_response(status_code=200, content=html.encode(), content_type="text/html", url=url)
            raise AssertionError(f"cross-origin, non-keyword JS must never be fetched: {url}")

        buf = io.StringIO()
        with patch("requests.get", side_effect=fake_get):
            with redirect_stdout(buf):
                result = diagnose_report_endpoints("sabic", 2024)
        self.assertEqual(result["external_js_files_fetched"], 0)

    def test_same_origin_generic_js_without_keyword_is_fetched(self):
        # (A) The actual SABIC case: a same-origin script with a generic
        # bundle name (main.js/templates.js/libs.js-style) carries no
        # report keyword in its filename, but must still be eligible for
        # inspection because it is served from the report page's own host.
        page_host = urlparse(self.PAGE_URL).scheme + "://" + urlparse(self.PAGE_URL).netloc
        generic_js_url = page_host + "/dist/js/main.js?v=2.5.1.1_2.46"
        html = f'''
        <html><body>
          <script src="/dist/js/main.js?v=2.5.1.1_2.46"></script>
        </body></html>
        '''
        js_content = b'fetch("/api/annual-reports/2024");'

        def fake_get(url, **kwargs):
            if url == self.PAGE_URL:
                return _fake_response(status_code=200, content=html.encode(), content_type="text/html", url=url)
            if url == generic_js_url:
                return _fake_response(status_code=200, content=js_content,
                                       content_type="application/javascript", url=url)
            raise AssertionError(f"unexpected URL requested: {url}")

        buf = io.StringIO()
        with patch("requests.get", side_effect=fake_get):
            with redirect_stdout(buf):
                result = diagnose_report_endpoints("sabic", 2024)
        self.assertEqual(result["external_js_files_fetched"], 1)
        external_js_candidates = [c for c in result["candidates"] if c["source"] == "external JS"]
        self.assertTrue(any(c["raw_value"] == "/api/annual-reports/2024" for c in external_js_candidates))

    def test_same_origin_js_fetch_cap_of_two_still_enforced(self):
        # (D) Even with the same-origin rule making every generic same-
        # origin script eligible, the existing cap of 2 fetched files
        # must still hold — no unbounded crawling.
        html = '''
        <html><body>
          <script src="/dist/js/main.js"></script>
          <script src="/dist/js/templates.js"></script>
          <script src="/dist/js/libs.js"></script>
        </body></html>
        '''
        fetched_js_urls = []

        def fake_get(url, **kwargs):
            if url == self.PAGE_URL:
                return _fake_response(status_code=200, content=html.encode(), content_type="text/html", url=url)
            fetched_js_urls.append(url)
            return _fake_response(status_code=200, content=b"// no report data here",
                                   content_type="application/javascript", url=url)

        buf = io.StringIO()
        with patch("requests.get", side_effect=fake_get):
            with redirect_stdout(buf):
                result = diagnose_report_endpoints("sabic", 2024)
        self.assertEqual(result["external_js_files_fetched"], 2)
        self.assertEqual(len(fetched_js_urls), 2)

    def test_no_high_confidence_endpoints_reports_no_reliable_path(self):
        html = '<html><body><a href="/investor/publications">Publications</a></body></html>'
        fake = _fake_response(status_code=200, content=html.encode(), content_type="text/html", url=self.PAGE_URL)
        buf = io.StringIO()
        with patch("requests.get", return_value=fake):
            with redirect_stdout(buf):
                diagnose_report_endpoints("sabic", 2024)
        output = buf.getvalue()
        self.assertIn("No reliable/verified path", output)


class TestDiagnoseReportEndpointsReportPageUrlOverride(unittest.TestCase):
    """Covers the optional report_page_url override: explicit-URL
    acceptance, unchanged default behavior, and that the override never
    mutates DIAGNOSTIC_REPORT_PAGE_CANDIDATES or any registry."""

    ARCHIVE_URL = "https://www.sabic.com/en/newsandmedia/media-centre-publications"

    def test_explicit_archive_url_is_fetched_instead_of_default(self):
        html = '<html><body><a href="/en/careers">Careers</a></body></html>'
        fake = _fake_response(status_code=200, content=html.encode(),
                               content_type="text/html", url=self.ARCHIVE_URL)

        def fake_get(url, **kwargs):
            if url == self.ARCHIVE_URL:
                return fake
            raise AssertionError(f"default sabic page should not be fetched: {url}")

        buf = io.StringIO()
        with patch("requests.get", side_effect=fake_get):
            with redirect_stdout(buf):
                result = diagnose_report_endpoints("sabic", 2024, report_page_url=self.ARCHIVE_URL)
        self.assertEqual(result["report_page_url"], self.ARCHIVE_URL)
        self.assertIn(self.ARCHIVE_URL, buf.getvalue())

    def test_omitting_override_keeps_default_company_url_unchanged(self):
        default_url = DIAGNOSTIC_REPORT_PAGE_CANDIDATES["sabic"]
        fake = _fake_response(status_code=200, content=b"<html></html>",
                               content_type="text/html", url=default_url)

        def fake_get(url, **kwargs):
            if url == default_url:
                return fake
            raise AssertionError(f"unexpected URL requested: {url}")

        buf = io.StringIO()
        with patch("requests.get", side_effect=fake_get):
            with redirect_stdout(buf):
                result = diagnose_report_endpoints("sabic", 2024)
        self.assertEqual(result["report_page_url"], default_url)

    def test_override_does_not_mutate_diagnostic_candidates_or_registry(self):
        candidates_before = dict(DIAGNOSTIC_REPORT_PAGE_CANDIDATES)
        from ingestion.load_historical import COMPANY_SOURCE_REGISTRIES
        import copy
        registries_before = copy.deepcopy(COMPANY_SOURCE_REGISTRIES)

        fake = _fake_response(status_code=200, content=b"<html></html>",
                               content_type="text/html", url=self.ARCHIVE_URL)
        buf = io.StringIO()
        with patch("requests.get", return_value=fake):
            with redirect_stdout(buf):
                diagnose_report_endpoints("sabic", 2024, report_page_url=self.ARCHIVE_URL)

        self.assertEqual(DIAGNOSTIC_REPORT_PAGE_CANDIDATES, candidates_before)
        self.assertEqual(COMPANY_SOURCE_REGISTRIES, registries_before)
        # Also confirm the override never leaked into the shared config dict.
        self.assertNotEqual(DIAGNOSTIC_REPORT_PAGE_CANDIDATES.get("sabic"), self.ARCHIVE_URL)

    def test_override_performs_no_real_network_io(self):
        # Same invariant as the rest of this file: everything is mocked.
        fake = _fake_response(status_code=200, content=b"<html></html>",
                               content_type="text/html", url=self.ARCHIVE_URL)
        buf = io.StringIO()
        with patch("requests.get", return_value=fake) as mock_get:
            with redirect_stdout(buf):
                diagnose_report_endpoints("sabic", 2024, report_page_url=self.ARCHIVE_URL)
        mock_get.assert_called_once()


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
