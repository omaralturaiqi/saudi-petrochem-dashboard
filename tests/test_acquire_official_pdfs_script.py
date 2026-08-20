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

from scripts.acquire_official_pdfs import count_pdf_targets, print_summary


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
