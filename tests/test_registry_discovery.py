"""
tests/test_registry_discovery.py

Offline tests for the multi-company Registry Discovery phase: validates
COMPANY_SOURCE_REGISTRIES as a whole (all real, currently-known companies)
rather than any single company's registry in isolation. No network, no
database, no PDF downloads — pure inspection of in-memory dicts.
"""
import unittest

from ingestion.load_historical import (
    ADVANCED_PETROCHEMICAL_SOURCE_REGISTRY,
    ALUJAIN_SOURCE_REGISTRY,
    COMPANY_SOURCE_REGISTRIES,
    CONFIRMED_COMPANY_TICKERS,
    NAMA_CHEMICALS_SOURCE_REGISTRY,
    PENDING_TICKER_CONFIRMATION,
    SABIC_AGRI_NUTRIENTS_SOURCE_REGISTRY,
    SABIC_SOURCE_REGISTRY,
    SAUDI_KAYAN_SOURCE_REGISTRY,
    SIIG_SOURCE_REGISTRY,
    SIPCHEM_SOURCE_REGISTRY,
    TASNEE_SOURCE_REGISTRY,
    VALID_SOURCE_TYPES,
    YANSAB_SOURCE_REGISTRY,
    describe_registry_coverage,
    get_registry_for_company,
)

# Single source of truth for expected tickers — reuses the module's own
# CONFIRMED_COMPANY_TICKERS rather than duplicating it, so this test file
# can't silently drift out of sync with the real registry.
EXPECTED_TICKER_BY_SLUG = CONFIRMED_COMPANY_TICKERS

PREVIOUSLY_PENDING_SLUGS = {
    "saudi_kayan", "sipchem", "tasnee", "siig",
    "nama_chemicals", "alujain", "sabic_agri_nutrients",
}

TARGET_FISCAL_YEARS = list(range(2015, 2025))

# The official base domain(s) (or Tier-2 saudiexchange.sa) each company's
# URLs are expected to live on — used to verify "every URL is tied to the
# correct company", not some other company's or a third party's domain.
# A URL's host matches if it equals the base domain or is a subdomain of it
# (e.g. "www.sabic.com" and "www3.sabic.com" both match base "sabic.com").
EXPECTED_DOMAINS_BY_SLUG = {
    "sabic": {"sabic.com"},
    "yansab": {"yansab.com.sa"},
    "advanced_petrochemical": {"advancedpetrochem.com"},
    "saudi_kayan": {"saudikayan.com"},
    "sipchem": {"sipchem.com"},
    "tasnee": {"tasnee.com"},
    "siig": {"siig.com.sa", "saudiexchange.sa"},
    "nama_chemicals": {"nama.com.sa"},
    "alujain": {"alujain.sa"},
    "sabic_agri_nutrients": {"sabic-agrinutrients.com"},
}


def _host_matches_domain(host, base_domain):
    return host == base_domain or host.endswith("." + base_domain)


class TestEachRegistryIsKeyedToItsOwnTicker(unittest.TestCase):
    def test_every_entry_in_every_registry_uses_the_companys_own_ticker(self):
        for slug, registry in COMPANY_SOURCE_REGISTRIES.items():
            expected_ticker = EXPECTED_TICKER_BY_SLUG[slug]
            for (ticker, fy) in registry:
                with self.subTest(slug=slug, fy=fy):
                    self.assertEqual(ticker, expected_ticker)

    def test_get_registry_for_company_returns_correct_object_per_slug(self):
        self.assertIs(get_registry_for_company("sabic"), SABIC_SOURCE_REGISTRY)
        self.assertIs(get_registry_for_company("yansab"), YANSAB_SOURCE_REGISTRY)
        self.assertIs(
            get_registry_for_company("advanced_petrochemical"),
            ADVANCED_PETROCHEMICAL_SOURCE_REGISTRY,
        )


class TestNoDuplicatesAcrossAllRegistries(unittest.TestCase):
    def test_no_duplicate_urls_within_any_single_registry(self):
        for slug, registry in COMPANY_SOURCE_REGISTRIES.items():
            urls = [e["source_url"] for e in registry.values()]
            with self.subTest(slug=slug):
                self.assertEqual(len(urls), len(set(urls)))

    def test_no_duplicate_urls_across_different_companies(self):
        # A URL registered for one company must never also appear under a
        # different company's registry — would indicate a copy/paste
        # source mixup.
        all_urls = []
        for registry in COMPANY_SOURCE_REGISTRIES.values():
            all_urls.extend(e["source_url"] for e in registry.values())
        self.assertEqual(len(all_urls), len(set(all_urls)))

    def test_no_duplicate_company_fiscal_year_keys_within_a_registry(self):
        # Dict keys are structurally unique by construction, but this
        # confirms no two DIFFERENT tuple keys collapse to the same
        # (company, fiscal_year) identity after normalization (e.g. no
        # accidental int/str fiscal_year mismatch creating a silent dup).
        for slug, registry in COMPANY_SOURCE_REGISTRIES.items():
            normalized = [(ticker, int(fy)) for (ticker, fy) in registry]
            with self.subTest(slug=slug):
                self.assertEqual(len(normalized), len(set(normalized)))

    def test_no_ticker_is_shared_across_two_different_companies(self):
        # Each company's ticker must be unique in the registry map — a
        # shared ticker would mean two companies' data could collide.
        tickers = list(EXPECTED_TICKER_BY_SLUG.values())
        self.assertEqual(len(tickers), len(set(tickers)))


class TestSourceTypeAndTierValidity(unittest.TestCase):
    def test_every_entry_has_a_supported_source_type(self):
        for slug, registry in COMPANY_SOURCE_REGISTRIES.items():
            for key, entry in registry.items():
                with self.subTest(slug=slug, key=key):
                    self.assertIn(entry.get("source_type"), VALID_SOURCE_TYPES)

    def test_every_entry_has_a_valid_source_tier(self):
        for slug, registry in COMPANY_SOURCE_REGISTRIES.items():
            for key, entry in registry.items():
                with self.subTest(slug=slug, key=key):
                    self.assertIn(entry.get("source_tier"), {1, 2, 3, 4})

    def test_every_entry_has_document_type_annual_report(self):
        for slug, registry in COMPANY_SOURCE_REGISTRIES.items():
            for key, entry in registry.items():
                with self.subTest(slug=slug, key=key):
                    self.assertEqual(entry.get("document_type"), "annual_report")

    def test_every_url_is_https(self):
        for slug, registry in COMPANY_SOURCE_REGISTRIES.items():
            for key, entry in registry.items():
                with self.subTest(slug=slug, key=key):
                    self.assertTrue(entry["source_url"].startswith("https://"))

    def test_every_url_is_hosted_on_the_correct_companys_domain(self):
        for slug, registry in COMPANY_SOURCE_REGISTRIES.items():
            allowed = EXPECTED_DOMAINS_BY_SLUG[slug]
            for key, entry in registry.items():
                host = entry["source_url"].split("/")[2]
                with self.subTest(slug=slug, key=key, host=host):
                    self.assertTrue(
                        any(_host_matches_domain(host, d) for d in allowed),
                        f"{host!r} is not a subdomain of any of {allowed!r} for {slug!r}",
                    )

    def test_no_url_hosted_on_another_registered_companys_domain(self):
        # A stronger cross-check than the positive test above: no entry's
        # host may match ANY OTHER company's allowed domain set.
        for slug, registry in COMPANY_SOURCE_REGISTRIES.items():
            other_domains = set()
            for other_slug, domains in EXPECTED_DOMAINS_BY_SLUG.items():
                if other_slug != slug:
                    other_domains |= domains
            for key, entry in registry.items():
                host = entry["source_url"].split("/")[2]
                with self.subTest(slug=slug, key=key, host=host):
                    self.assertFalse(
                        any(_host_matches_domain(host, d) for d in other_domains),
                        f"{host!r} in {slug!r}'s registry matches another company's domain",
                    )


class TestTickerVerificationInvariants(unittest.TestCase):
    """Covers the ticker-verification round for the 7 previously-pending
    companies (Saudi Kayan, Sipchem, Tasnee, SIIG, Nama Chemicals, Alujain,
    SABIC Agri-Nutrients), now resolved via WebSearch with multi-source
    corroboration. Confirms every ticker is real, non-empty, non-duplicate,
    non-placeholder, and that all 7 companies are now fully wired into
    COMPANY_SOURCE_REGISTRIES with PENDING_TICKER_CONFIRMATION emptied."""

    PLACEHOLDER_MARKERS = ("TBD", "PLACEHOLDER", "XXXX", "UNKNOWN", "N/A", "TODO", "FIXME")

    def test_every_confirmed_ticker_is_non_empty(self):
        for slug, ticker in EXPECTED_TICKER_BY_SLUG.items():
            with self.subTest(slug=slug):
                self.assertTrue(ticker)
                self.assertIsInstance(ticker, str)

    def test_no_confirmed_ticker_looks_like_a_placeholder(self):
        for slug, ticker in EXPECTED_TICKER_BY_SLUG.items():
            with self.subTest(slug=slug):
                upper = ticker.upper()
                for marker in self.PLACEHOLDER_MARKERS:
                    self.assertNotIn(marker, upper)

    def test_no_ticker_duplicated_across_all_ten_companies(self):
        tickers = list(EXPECTED_TICKER_BY_SLUG.values())
        self.assertEqual(len(tickers), len(set(tickers)))
        self.assertEqual(len(tickers), 10)

    def test_all_seven_previously_pending_companies_are_now_resolved(self):
        for slug in PREVIOUSLY_PENDING_SLUGS:
            with self.subTest(slug=slug):
                self.assertIn(slug, EXPECTED_TICKER_BY_SLUG)
                self.assertIn(slug, COMPANY_SOURCE_REGISTRIES)

    def test_pending_ticker_confirmation_is_now_empty(self):
        self.assertEqual(PENDING_TICKER_CONFIRMATION, {})

    def test_no_company_remains_in_pending_state(self):
        for slug in PREVIOUSLY_PENDING_SLUGS:
            with self.subTest(slug=slug):
                self.assertNotIn(slug, PENDING_TICKER_CONFIRMATION)

    def test_company_source_registries_contains_all_verified_companies(self):
        self.assertEqual(set(COMPANY_SOURCE_REGISTRIES), set(EXPECTED_TICKER_BY_SLUG))

    def test_get_registry_for_company_resolves_all_seven_newly_verified_companies(self):
        # Report-URL discovery has since populated some of these; the
        # important invariant is that all 7 resolve without KeyError.
        for slug in PREVIOUSLY_PENDING_SLUGS:
            with self.subTest(slug=slug):
                registry = get_registry_for_company(slug)
                self.assertIsInstance(registry, dict)


class TestOfficialSourceRegistryDiscovery(unittest.TestCase):
    """Covers this round's Official Source Registry Discovery pass: every
    company/year has a clear, traceable status (FOUND-as-pdf,
    FOUND-as-report_page, or MISSING), and nothing was guessed."""

    PLACEHOLDER_MARKERS = ("TBD", "PLACEHOLDER", "XXXX", "EXAMPLE", "REPLACE_ME", "FAKE")

    def test_sabic_agri_nutrients_registry_remains_empty(self):
        # No fiscal-year-specific Annual Report PDF/page could be verified
        # for this company in this round (only an ambiguous "ER" document
        # was found, not confirmed to be the annual report) — left empty
        # rather than guessed.
        self.assertEqual(SABIC_AGRI_NUTRIENTS_SOURCE_REGISTRY, {})

    def test_sabic_fy2015_remains_unregistered(self):
        self.assertNotIn(("2010", 2015), SABIC_SOURCE_REGISTRY)

    def test_every_company_and_year_has_a_clear_status(self):
        # describe_registry_coverage() must return exactly one row per
        # requested fiscal year for every company — no gaps, no crashes.
        for slug, registry in COMPANY_SOURCE_REGISTRIES.items():
            ticker = EXPECTED_TICKER_BY_SLUG[slug]
            rows = describe_registry_coverage(TARGET_FISCAL_YEARS, ticker=ticker, registry=registry)
            with self.subTest(slug=slug):
                self.assertEqual(len(rows), len(TARGET_FISCAL_YEARS))
                for row in rows:
                    self.assertIn(row["found"], (True, False))
                    if row["found"]:
                        self.assertIn(row["source_type"], VALID_SOURCE_TYPES)
                    else:
                        self.assertIsNone(row["source_type"])

    def test_no_url_looks_guessed_or_placeholder(self):
        for slug, registry in COMPANY_SOURCE_REGISTRIES.items():
            for key, entry in registry.items():
                url_upper = entry["source_url"].upper()
                with self.subTest(slug=slug, key=key):
                    for marker in self.PLACEHOLDER_MARKERS:
                        self.assertNotIn(marker, url_upper)

    def test_no_generic_index_page_registered_as_a_specific_years_report_page(self):
        # Sipchem's /en/reports and Advanced Petrochemical's
        # /financial-information/ are known generic archive pages found
        # during discovery — confirms neither was mistakenly registered.
        sipchem_urls = {e["source_url"] for e in SIPCHEM_SOURCE_REGISTRY.values()}
        self.assertNotIn("https://www.sipchem.com/en/reports", sipchem_urls)
        advanced_urls = {e["source_url"] for e in ADVANCED_PETROCHEMICAL_SOURCE_REGISTRY.values()}
        self.assertFalse(any("financial-information" in u for u in advanced_urls))


class TestRegistryIsolationAcrossCompanies(unittest.TestCase):
    def test_registries_are_distinct_dict_objects(self):
        registries = list(COMPANY_SOURCE_REGISTRIES.values())
        for i, reg_a in enumerate(registries):
            for reg_b in registries[i + 1:]:
                self.assertIsNot(reg_a, reg_b)

    def test_mutating_one_companys_registry_copy_does_not_affect_another(self):
        sabic_copy = dict(SABIC_SOURCE_REGISTRY)
        sabic_copy[("2010", 1900)] = {"source_url": "https://sabic.com/fake.pdf"}
        self.assertNotIn(("2010", 1900), YANSAB_SOURCE_REGISTRY)
        self.assertNotIn(("2010", 1900), ADVANCED_PETROCHEMICAL_SOURCE_REGISTRY)
        self.assertNotIn(("2010", 1900), SABIC_SOURCE_REGISTRY)  # copy, not the real dict


if __name__ == "__main__":
    unittest.main()
