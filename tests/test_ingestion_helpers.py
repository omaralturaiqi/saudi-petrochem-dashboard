"""
tests/test_ingestion_helpers.py

Offline, deterministic tests for ingestion/load_historical.py's
preparation-level logic. None of these tests touch Neon, the network, or
any downloaded file — they use only in-memory data and small temp files
created by the test itself.

Run with:  python -m pytest tests/  (or python -m unittest discover tests)
"""
import hashlib
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ingestion.load_historical import (
    CandidateFinancialFact,
    build_duplicate_key,
    sha256_file,
    validate_concept,
    validate_confidence,
    validate_period,
    validate_restatement_rule,
    validate_statement_type,
)


class TestSha256File(unittest.TestCase):
    def test_matches_hashlib_directly(self):
        content = b"some fake PDF bytes for testing, not a real report"
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(content)
            path = Path(f.name)
        try:
            expected = hashlib.sha256(content).hexdigest()
            self.assertEqual(sha256_file(path), expected)
        finally:
            path.unlink()

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = Path(f.name)
        try:
            self.assertEqual(sha256_file(path), hashlib.sha256(b"").hexdigest())
        finally:
            path.unlink()

    def test_different_content_different_hash(self):
        with tempfile.NamedTemporaryFile(delete=False) as f1:
            f1.write(b"content A")
            path1 = Path(f1.name)
        with tempfile.NamedTemporaryFile(delete=False) as f2:
            f2.write(b"content B")
            path2 = Path(f2.name)
        try:
            self.assertNotEqual(sha256_file(path1), sha256_file(path2))
        finally:
            path1.unlink()
            path2.unlink()


class TestValidatePeriod(unittest.TestCase):
    def test_valid_period_with_start_and_end(self):
        self.assertEqual(
            validate_period(date(2024, 1, 1), date(2024, 12, 31)), []
        )

    def test_valid_period_start_none(self):
        # schema.sql allows period_start to be NULL — must not be flagged
        self.assertEqual(validate_period(None, date(2024, 12, 31)), [])

    def test_missing_period_end(self):
        errors = validate_period(date(2024, 1, 1), None)
        self.assertEqual(len(errors), 1)
        self.assertIn("period_end", errors[0])

    def test_start_after_end_is_rejected(self):
        errors = validate_period(date(2024, 12, 31), date(2024, 1, 1))
        self.assertEqual(len(errors), 1)
        self.assertIn("after", errors[0])

    def test_start_equal_end_is_valid(self):
        d = date(2024, 6, 30)
        self.assertEqual(validate_period(d, d), [])


class TestValidateConcept(unittest.TestCase):
    def test_known_concept_passes(self):
        self.assertEqual(validate_concept("revenue"), [])

    def test_unknown_concept_flagged(self):
        errors = validate_concept("totally_made_up_concept")
        self.assertEqual(len(errors), 1)
        self.assertIn("totally_made_up_concept", errors[0])

    def test_empty_concept_flagged(self):
        errors = validate_concept("")
        self.assertEqual(len(errors), 1)
        self.assertIn("empty", errors[0])

    def test_custom_known_set_respected(self):
        # a caller can pass a freshly-queried live set instead of the
        # module's point-in-time snapshot
        custom = frozenset({"some_new_concept"})
        self.assertEqual(validate_concept("some_new_concept", custom), [])
        self.assertNotEqual(validate_concept("revenue", custom), [])


class TestValidateStatementType(unittest.TestCase):
    def test_all_schema_values_accepted(self):
        for st in (
            "income_statement", "balance_sheet", "cash_flow",
            "equity_changes", "segment", "other",
        ):
            self.assertEqual(validate_statement_type(st), [])

    def test_invalid_value_rejected(self):
        self.assertNotEqual(validate_statement_type("not_a_real_statement"), [])


class TestValidateConfidence(unittest.TestCase):
    def test_valid_values(self):
        for c in ("HIGH", "MEDIUM", "LOW"):
            self.assertEqual(validate_confidence(c), [])

    def test_invalid_value(self):
        # UNRESOLVED exists in us_xbrl's vocabulary but is NOT valid for
        # core.financial_line_items per schema.sql's CHECK constraint
        self.assertNotEqual(validate_confidence("UNRESOLVED"), [])


class TestBuildDuplicateKey(unittest.TestCase):
    def _fact(self, **overrides):
        base = dict(
            company_ticker="2010",
            statement_type="income_statement",
            concept="revenue",
            reported_label="Revenue",
            fiscal_year=2024,
            period_end=date(2024, 12, 31),
            value_raw=139980500.0,
            unit="thousand",
            extraction_method="test_method",
            confidence="HIGH",
        )
        base.update(overrides)
        return CandidateFinancialFact(**base)

    def test_identical_facts_produce_identical_keys(self):
        f1 = self._fact()
        f2 = self._fact(reported_label="a different label, same identity")
        self.assertEqual(build_duplicate_key(f1), build_duplicate_key(f2))

    def test_different_fiscal_year_produces_different_key(self):
        f1 = self._fact(fiscal_year=2024)
        f2 = self._fact(fiscal_year=2023)
        self.assertNotEqual(build_duplicate_key(f1), build_duplicate_key(f2))

    def test_different_concept_produces_different_key(self):
        f1 = self._fact(concept="revenue")
        f2 = self._fact(concept="net_income")
        self.assertNotEqual(build_duplicate_key(f1), build_duplicate_key(f2))

    def test_quarterly_vs_annual_differ(self):
        f1 = self._fact(fiscal_quarter=None)
        f2 = self._fact(fiscal_quarter=2)
        self.assertNotEqual(build_duplicate_key(f1), build_duplicate_key(f2))

    def test_different_period_type_produces_different_key(self):
        # A half-year fact and an annual fact for the same
        # (company, concept, fiscal_year) with fiscal_quarter=None must NOT
        # collide just because fiscal_quarter matches — period_type is a
        # real, confirmed NOT NULL live column and must be part of the key.
        f1 = self._fact(period_type="FY", fiscal_quarter=None)
        f2 = self._fact(period_type="H", fiscal_quarter=None)
        self.assertNotEqual(build_duplicate_key(f1), build_duplicate_key(f2))

    def test_different_fiscal_half_produces_different_key(self):
        f1 = self._fact(period_type="H", fiscal_half=1)
        f2 = self._fact(period_type="H", fiscal_half=2)
        self.assertNotEqual(build_duplicate_key(f1), build_duplicate_key(f2))

    def test_same_period_type_and_fiscal_half_match(self):
        f1 = self._fact(period_type="FY", fiscal_half=None)
        f2 = self._fact(period_type="FY", fiscal_half=None, reported_label="diff label")
        self.assertEqual(build_duplicate_key(f1), build_duplicate_key(f2))


class TestValidateRestatementRule(unittest.TestCase):
    def test_brand_new_fact_no_restatement_claim_is_valid(self):
        errors = validate_restatement_rule(
            existing_fact_exists=False,
            proposed_is_restated=None,
            proposed_restated_from_id=None,
        )
        self.assertEqual(errors, [])

    def test_restated_true_without_parent_id_rejected(self):
        errors = validate_restatement_rule(
            existing_fact_exists=True,
            proposed_is_restated=True,
            proposed_restated_from_id=None,
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("restated_from_line_item_id is missing", errors[0])

    def test_restated_true_with_parent_id_and_existing_fact_is_valid(self):
        errors = validate_restatement_rule(
            existing_fact_exists=True,
            proposed_is_restated=True,
            proposed_restated_from_id="some-uuid",
        )
        self.assertEqual(errors, [])

    def test_restatement_claim_with_no_existing_fact_rejected(self):
        errors = validate_restatement_rule(
            existing_fact_exists=False,
            proposed_is_restated=None,
            proposed_restated_from_id="some-uuid",
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("no existing prior fact was found", errors[0])


if __name__ == "__main__":
    unittest.main()
