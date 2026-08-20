"""
ingestion/load_historical.py

Phase 1 (Historical Data Layer) — GENERIC, multi-company ACQUISITION layer;
everything else remains scaffolding only.

Acquisition (acquire_reports() and its helpers below) is real, runnable
code: it can genuinely fetch a PDF given a verified URL, hash it, validate
it, and record it in a local JSON manifest under data/raw/<company_slug>/.
It does NOT connect to any database, execute SQL, or write to Neon in any
way — its entire effect is local files under data/raw/ plus the manifest.

ARCHITECTURE — generic layer vs. company configuration:
  acquire_one_report(), acquire_reports(), and describe_registry_coverage()
  are entirely GENERIC: none of them reference any specific company, ticker,
  or URL by name, and none of them contain company-specific branching.
  They operate purely on whatever `registry` dict is handed to them (shape:
  {(ticker, fiscal_year): {source_url, source_type, source_tier,
  document_type, ...}}).

  SABIC_SOURCE_REGISTRY is CONFIGURATION DATA for one company, sitting on
  top of that generic layer — not something the acquisition functions know
  about. COMPANY_SOURCE_REGISTRIES maps company_slug -> that company's own
  registry dict; get_registry_for_company() resolves one by name. Adding a
  new company is purely a data change (define its registry dict in the same
  shape, add one line to COMPANY_SOURCE_REGISTRIES) — it never requires
  touching acquire_one_report/acquire_reports/describe_registry_coverage.

It also does NOT extract financial facts, does NOT use parser.py, and does
NOT create financial_line_items rows — that remains explicitly out of scope
(see extract_facts/load_facts TODOs further below, still unimplemented).

Everything below the acquisition section is pure Python — no `requests`
calls, no database connection, no filesystem I/O beyond hashing a file the
caller already has on disk. Nothing in this file runs automatically on
import; acquire_reports() must be called explicitly.

WHAT STILL NEEDS TO BE BUILT (not in scope for this task):
  - extraction (parsing a fetched PDF into candidate financial facts) —
    this project's existing parser.py is the starting point, but note:
    none of the current 62 live core.financial_line_items rows were
    produced by parser.py's extraction_method — see the "Known
    reproducibility gap" note below.
  - normalization / validation-in-anger against the live dictionary
  - the actual database loader that turns validated candidate rows into
    real INSERT statements against core.financial_line_items /
    core.source_documents, run only after explicit human review.
  - Wiring SOURCE_TIER / period_type / fiscal_half real-world values once
    their full domain is confirmed beyond what's been observed so far
    (see PERIOD_TYPE_OBSERVED_VALUES below).

Known reproducibility gap (documented, not solved here):
  The current 62 live core.financial_line_items rows were produced by
  extraction_method values ('matcher_v5_column_aware',
  'manual_verification+parser_v2', 'manual_verification+arithmetic_cross_check')
  that do not exist anywhere in this repository's parser.py. This pipeline
  is intended to become the canonical, version-controlled path going
  forward — it is explicitly NOT an attempt to reverse-engineer or
  reproduce those specific 62 rows.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional


# ============================================================================
# Constants
# ============================================================================

# Source-tier hierarchy (documentation of core.source_documents.source_tier's
# intended meaning — the column itself already exists with a
# CHECK (source_tier BETWEEN 1 AND 4) constraint in schema.sql; this dict is
# the human-readable meaning that was never written down anywhere before).
SOURCE_TIERS = {
    1: "Primary official company disclosure (annual report, official "
       "financial statements, investor-relations publication)",
    2: "Official Saudi Exchange (Tadawul) / CMA re-publication of the "
       "company's own filing",
    3: "Secondary normalized source — cross-validation only, never sole "
       "source of a fact",
    4: "Derived/calculated — NEVER written to financial_line_items; "
       "belongs exclusively in derived_metrics",
}

# The only period_type value actually observed live, as of Phase 1
# preparation (62/62 existing rows). This is NOT a declaration that these
# are the only valid values — it is a record of what has been directly
# confirmed by query. Do not add 'Q' or 'H' here speculatively; add them
# only once actually observed or explicitly decided and documented.
PERIOD_TYPE_OBSERVED_VALUES = frozenset({"FY"})

# Statement types, mirrored from schema.sql's CHECK constraint on
# financial_line_items.statement_type — kept here so validation logic (see
# below) doesn't have to re-derive it from a live query every time.
VALID_STATEMENT_TYPES = frozenset({
    "income_statement", "balance_sheet", "cash_flow", "equity_changes",
    "segment", "other",
})

VALID_CONFIDENCE_VALUES = frozenset({"HIGH", "MEDIUM", "LOW"})
VALID_UNIT_VALUES = frozenset({"unit", "thousand", "million"})

# The 19 concepts confirmed live in core.concept_dictionary as of Phase 1
# preparation. This list exists so validation stubs below can check against
# something concrete offline; it is NOT a substitute for querying the live
# dictionary before any real ingestion run, since it could drift further.
CONFIRMED_LIVE_CONCEPTS = frozenset({
    "capex", "cash_and_equivalents", "cfo", "cost_of_revenue",
    "eps_basic", "eps_basic_continuing", "eps_basic_total",
    "equity_attributable_to_parent", "gross_profit", "net_income",
    "net_income_attributable_to_parent", "net_income_continuing",
    "net_income_continuing_attributable_to_parent", "net_income_total",
    "operating_income", "revenue", "total_assets", "total_equity",
    "total_liabilities",
})


# ============================================================================
# Data models
# ============================================================================

@dataclass(frozen=True)
class SourceDocumentMetadata:
    """Mirrors core.source_documents' columns. Constructing one of these
    does NOT insert anything — it's a plain, immutable data holder for the
    future loader to validate and (eventually, after human review) turn
    into an actual INSERT."""
    company_ticker: str
    document_type: str  # must be one of source_documents' CHECK values
    fiscal_year: int
    source_url: str
    source_website: str
    source_tier: int  # 1-4, see SOURCE_TIERS above
    document_sha256: str
    fiscal_quarter: Optional[int] = None
    report_period_end: Optional[date] = None
    publication_date: Optional[date] = None
    page_count: Optional[int] = None
    supersedes_document_id: Optional[str] = None


@dataclass(frozen=True)
class CandidateFinancialFact:
    """Mirrors core.financial_line_items' columns for a not-yet-validated,
    not-yet-inserted candidate row. Nothing about constructing this object
    touches the database."""
    company_ticker: str
    statement_type: str
    concept: str
    reported_label: str
    fiscal_year: int
    period_end: date
    value_raw: float
    unit: str
    extraction_method: str
    confidence: str
    fiscal_quarter: Optional[int] = None
    period_start: Optional[date] = None
    currency: str = "SAR"
    source_page: Optional[int] = None
    raw_text: Optional[str] = None
    parser_version: Optional[str] = None
    reviewed_by_human: bool = False
    review_notes: Optional[str] = None
    is_restated: Optional[bool] = None
    restated_from_line_item_id: Optional[str] = None
    # period_type / fiscal_half exist live but their full validated domain
    # isn't established yet — see PERIOD_TYPE_OBSERVED_VALUES above.
    period_type: Optional[str] = None
    fiscal_half: Optional[int] = None


# ============================================================================
# SHA-256 helper (reused pattern from parser.py's sha256_file, kept
# consistent so a document hashed by either code path produces the same
# digest and the DB's UNIQUE(document_sha256) constraint dedupes correctly)
# ============================================================================

def sha256_file(path: Path) -> str:
    """Compute the SHA-256 hex digest of a file's contents. Pure I/O read,
    no network, no database. Same chunked-read pattern as parser.py's
    existing sha256_file() to guarantee identical digests for identical
    files regardless of which code path hashed them."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# ============================================================================
# Validation stubs — pure functions, no I/O, safe to unit test offline
# ============================================================================

def validate_period(period_start: Optional[date], period_end: date) -> list[str]:
    """Return a list of human-readable validation errors (empty list =
    valid). Never raises for bad input — callers decide what to do with
    quarantine-worthy records."""
    errors: list[str] = []
    if period_end is None:
        errors.append("period_end is required and was None")
        return errors
    if period_start is not None and period_start > period_end:
        errors.append(
            f"period_start ({period_start}) is after period_end ({period_end})"
        )
    return errors


def validate_concept(concept: str, known_concepts: frozenset[str] = CONFIRMED_LIVE_CONCEPTS) -> list[str]:
    """Checks a candidate concept key against the last-confirmed live
    dictionary. NOTE: known_concepts defaults to a point-in-time snapshot
    (CONFIRMED_LIVE_CONCEPTS) — a real ingestion run should pass in a
    freshly-queried set instead of relying on the default, since the live
    dictionary can change independently of this file."""
    errors: list[str] = []
    if not concept or not concept.strip():
        errors.append("concept is empty")
        return errors
    if concept not in known_concepts:
        errors.append(
            f"concept '{concept}' is not in the known concept_dictionary set "
            "— confirm against a live query before treating this as a real gap"
        )
    return errors


def validate_statement_type(statement_type: str) -> list[str]:
    if statement_type not in VALID_STATEMENT_TYPES:
        return [
            f"statement_type '{statement_type}' is not one of "
            f"{sorted(VALID_STATEMENT_TYPES)}"
        ]
    return []


def validate_confidence(confidence: str) -> list[str]:
    if confidence not in VALID_CONFIDENCE_VALUES:
        return [
            f"confidence '{confidence}' is not one of "
            f"{sorted(VALID_CONFIDENCE_VALUES)}"
        ]
    return []


def build_duplicate_key(fact: CandidateFinancialFact) -> tuple:
    """Builds the tuple used to detect an exact-duplicate candidate before
    it's ever sent toward an INSERT — mirrors the
    (company_id, concept, fiscal_year, fiscal_quarter, statement_type,
    document_id) grouping used in the live-audit duplicate-detection
    queries, PLUS period_type and fiscal_half (both confirmed live columns
    not covered by the original live-audit queries, which predate their
    discovery). Without these two, a half-year fact and an annual fact for
    the same (company, concept, fiscal_year) with fiscal_quarter=NULL would
    collide under this key and be wrongly flagged as duplicates — including
    them is required, not optional, given period_type is a real
    NOT NULL column. Uses company_ticker here since document_id doesn't
    exist until a source_documents row is actually registered; the loader
    should resolve ticker -> company_id and re-key once that's available."""
    return (
        fact.company_ticker,
        fact.concept,
        fact.fiscal_year,
        fact.fiscal_quarter,
        fact.statement_type,
        fact.period_type,
        fact.fiscal_half,
    )


def validate_restatement_rule(
    existing_fact_exists: bool,
    proposed_is_restated: Optional[bool],
    proposed_restated_from_id: Optional[str],
) -> list[str]:
    """Encodes the append-only restatement rule as a pure check:
      - A brand-new fact (no existing row for this identity) should not
        claim is_restated=True with no parent — that would be a fabricated
        restatement, not a real one.
      - Any row claiming is_restated=True MUST carry a
        restated_from_line_item_id pointing at the original.
    This does NOT touch the database — it only validates the *shape* of a
    proposed insert before the loader (not yet built) would ever send it.
    """
    errors: list[str] = []
    if proposed_is_restated and not proposed_restated_from_id:
        errors.append(
            "is_restated=True but restated_from_line_item_id is missing — "
            "every restated row must reference the fact it restates"
        )
    if proposed_restated_from_id and not existing_fact_exists:
        errors.append(
            "restated_from_line_item_id set but no existing prior fact was "
            "found for this identity — cannot restate something that was "
            "never loaded"
        )
    return errors


# ============================================================================
# Coverage-report query definitions
# ============================================================================
# These are SQL TEXT ONLY — string constants, not executed by this module.
# They exist so the eventual coverage-report tooling (and this session's
# future live-audit rounds) can reuse a single, reviewed source of truth
# instead of hand-retyping the query each time. Nothing in this file runs
# these against any database connection.

COVERAGE_REPORT_QUERY = """
SELECT
    c.ticker, c.name_en, c.status,
    MIN(f.fiscal_year) AS earliest_fy,
    MAX(f.fiscal_year) AS latest_fy,
    COUNT(f.line_item_id) AS total_facts,
    COUNT(DISTINCT f.concept) AS distinct_concepts,
    COUNT(f.line_item_id) FILTER (WHERE f.fiscal_quarter IS NOT NULL) AS quarterly_facts
FROM core.companies c
LEFT JOIN core.financial_line_items f ON f.company_id = c.company_id
GROUP BY c.ticker, c.name_en, c.status
ORDER BY c.ticker;
"""

DUPLICATE_CHECK_QUERY = """
SELECT company_id, concept, fiscal_year, fiscal_quarter, statement_type,
       period_type, fiscal_half, document_id, COUNT(*) AS n
FROM core.financial_line_items
GROUP BY company_id, concept, fiscal_year, fiscal_quarter, statement_type,
         period_type, fiscal_half, document_id
HAVING COUNT(*) > 1;
"""
# NOTE: period_type/fiscal_half were added to this GROUP BY during
# self-review — the original live-audit duplicate-check queries (run before
# these two columns were confirmed to exist) did not include them, and
# omitting them here would let a half-year fact and an annual fact for the
# same (company, concept, fiscal_year) collide under one group.


# ============================================================================
# ACQUISITION LAYER
# ============================================================================
# Everything below is real, runnable code. It reads/writes local files under
# data/raw/<ticker>/ and a JSON manifest — nothing else. It never touches
# Neon, never runs SQL, never extracts a financial fact.
# ============================================================================

RAW_DATA_ROOT = Path(__file__).resolve().parent.parent / "data" / "raw"

# --- Known-URL registry -----------------------------------------------------
# Maps (ticker, fiscal_year) -> a VERIFIED, human-confirmed source URL.
# This registry starts EMPTY on purpose. Per the explicit "do not invent a
# URL" rule, acquire_reports() will NOT guess a report location for any
# year that has no entry here — it only ever fetches URLs that a human (or
# a prior, explicitly-approved discovery step) has actually verified and
# added below. Populating this dict is a separate, future step, not part
# of this task.
SABIC_SOURCE_REGISTRY: dict[tuple[str, int], dict] = {
    # Verified, human-confirmed official SABIC.com sources. Only years with
    # an entry here are considered "sourced" at all — see the module-level
    # note above. Each entry's "source_type" is either:
    #   "pdf"          — a direct, verified link to the actual PDF report.
    #                    acquire_one_report() will (in a future real run)
    #                    fetch and hash this file.
    #   "report_page"  — a verified official SABIC report-listing page
    #                    where the actual PDF has NOT yet been identified.
    #                    This is recorded as a known SOURCE, never treated
    #                    as an acquired document: no download is attempted
    #                    from it, no SHA-256 is computed, and it can never
    #                    result in acquisition_status='AVAILABLE'.
    # FY2015 is deliberately ABSENT: no verified official SABIC source has
    # been confirmed for it, and per the no-guessing rule none was
    # invented. It remains MISSING until a real verified source is added.
    ("2010", 2016): {
        "source_url": "https://www.sabic.com/en/Images/SABIC-Annual-Report-2016-English_tcm1010-6151.pdf",
        "source_website": "sabic.com",
        "source_tier": 1,
        "document_type": "annual_report",
        "source_type": "pdf",
    },
    ("2010", 2017): {
        "source_url": "https://www.sabic.com/zh/Images/SABIC-Annual-Report-ENGLISH_tcm11-12625.pdf",
        "source_website": "sabic.com",
        "source_tier": 1,
        "document_type": "annual_report",
        "source_type": "pdf",
    },
    ("2010", 2018): {
        "source_url": "https://www.sabic.com/en/Images/SABIC-AR-English-2018_tcm1010-18629.pdf",
        "source_website": "sabic.com",
        "source_tier": 1,
        "document_type": "annual_report",
        "source_type": "pdf",
    },
    ("2010", 2019): {
        "source_url": "https://www.sabic.com/en/reports/annual-2019",
        "source_website": "sabic.com",
        "source_tier": 1,
        "document_type": "annual_report",
        "source_type": "report_page",
    },
    ("2010", 2020): {
        "source_url": "https://www.sabic.com/en/reports/annual-2020",
        "source_website": "sabic.com",
        "source_tier": 1,
        "document_type": "annual_report",
        "source_type": "report_page",
    },
    ("2010", 2021): {
        "source_url": "https://www.sabic.com/en/reports/annual-2021",
        "source_website": "sabic.com",
        "source_tier": 1,
        "document_type": "annual_report",
        "source_type": "report_page",
    },
    ("2010", 2022): {
        "source_url": "https://www.sabic.com/en/Images/Sabic-Annual-Report-2022-EN_tcm1010-38980.pdf",
        "source_website": "sabic.com",
        "source_tier": 1,
        "document_type": "annual_report",
        "source_type": "pdf",
    },
    ("2010", 2023): {
        "source_url": "https://www.sabic.com/en/Images/SABIC-Integrated-Annual-Report-2023-EN-Updated_tcm1010-42927.pdf",
        "source_website": "sabic.com",
        "source_tier": 1,
        "document_type": "annual_report",
        "source_type": "pdf",
    },
    ("2010", 2024): {
        "source_url": "https://www3.sabic.com/en/Images/SABIC-Integrated-Annual-Report-2024-EN-Updated_tcm1010-46870.pdf",
        "source_website": "sabic.com",
        "source_tier": 1,
        "document_type": "annual_report",
        "source_type": "pdf",
    },
}

# --- Multi-company registry abstraction -------------------------------------
# The acquisition functions below (acquire_one_report, acquire_reports,
# describe_registry_coverage) are GENERIC — they take a `registry` dict as
# an explicit argument and contain no company-specific branching anywhere.
# SABIC_SOURCE_REGISTRY above is just one company's CONFIGURATION sitting on
# top of that generic layer, not something the acquisition logic knows
# about by name. Adding a new company is purely a data change: define its
# own `{(ticker, fiscal_year): {...}}` dict (same shape as
# SABIC_SOURCE_REGISTRY) and register it here — no change to
# acquire_one_report/acquire_reports/describe_registry_coverage is ever
# required to onboard a new company.

# ----------------------------------------------------------------------------
# YANSAB — ticker "2290", confirmed in schema/README/live data (already the
# subject of loaded financial facts and a registered source_documents row
# for FY2024). Registry Discovery for this task's environment could not
# verify any official annual-report URL for ANY fiscal year: this session's
# network egress was blocked to yansab.com.sa in prior rounds (direct
# curl/WebFetch). THIS round used WebSearch instead (independent
# infrastructure) and found several direct, official yansab.com.sa PDF
# URLs. Only years with a verified, year-specific PDF are registered below
# — every other year (2015, 2016, 2018, 2020, 2021, 2023) had no verified
# URL surfaced in search results and is left absent (-> MISSING).
# ----------------------------------------------------------------------------
YANSAB_SOURCE_REGISTRY: dict[tuple[str, int], dict] = {
    ("2290", 2017): {
        "source_url": "https://www.yansab.com.sa/ar/Images/YANSAB-Annual-Report-2017-ar_tcm1048-12444.pdf",
        "source_website": "yansab.com.sa", "source_tier": 1,
        "document_type": "annual_report", "source_type": "pdf",
    },
    ("2290", 2019): {
        "source_url": "https://www.yansab.com.sa/ar/Images/Annual-Report-2019Ar_tcm1048-24786.pdf",
        "source_website": "yansab.com.sa", "source_tier": 1,
        "document_type": "annual_report", "source_type": "pdf",
    },
    ("2290", 2022): {
        "source_url": "https://www.yansab.com.sa/en/Images/Yansab-Annual-Report-EN-2022_tcm1047-38873.pdf",
        "source_website": "yansab.com.sa", "source_tier": 1,
        "document_type": "annual_report", "source_type": "pdf",
    },
    ("2290", 2024): {
        "source_url": "https://www.yansab.com.sa/en/Images/Yansab%20Annual%20Report%202024%20-EN_tcm1047-46828.pdf",
        "source_website": "yansab.com.sa", "source_tier": 1,
        "document_type": "annual_report", "source_type": "pdf",
    },
}

# ----------------------------------------------------------------------------
# Advanced Petrochemical Company — ticker "2330". WebSearch found direct,
# official ir.advancedpetrochem.com PDFs for FY2022-FY2024 only; no
# verified URLs surfaced for FY2015-FY2021 (the IR site's general
# "Financial Information" landing page was found but is not year-specific,
# so it was NOT registered as a report_page — would misrepresent a generic
# index as a per-year verified source).
# ----------------------------------------------------------------------------
ADVANCED_PETROCHEMICAL_SOURCE_REGISTRY: dict[tuple[str, int], dict] = {
    ("2330", 2022): {
        "source_url": "https://ir.advancedpetrochem.com/media/aaofmcjt/2022-en-annual-report.pdf",
        "source_website": "ir.advancedpetrochem.com", "source_tier": 1,
        "document_type": "annual_report", "source_type": "pdf",
    },
    ("2330", 2023): {
        "source_url": "https://ir.advancedpetrochem.com/media/auoc21zt/2023_annual_report_en.pdf",
        "source_website": "ir.advancedpetrochem.com", "source_tier": 1,
        "document_type": "annual_report", "source_type": "pdf",
    },
    ("2330", 2024): {
        "source_url": "https://ir.advancedpetrochem.com/media/3donny2s/annual-report-2024-en.pdf",
        "source_website": "ir.advancedpetrochem.com", "source_tier": 1,
        "document_type": "annual_report", "source_type": "pdf",
    },
}

# ----------------------------------------------------------------------------
# The 7 previously-pending companies — tickers verified in the prior round.
# This round (Official Source Registry Discovery) used WebSearch to find
# report URLs. Argaam/argaamplus.s3.amazonaws.com results were treated as
# Tier 3 (verification-only, never registered as the primary source) per
# instruction. Only direct hits on each company's own official domain, or
# saudiexchange.sa when the company's own site had no direct PDF, were
# registered.
# ----------------------------------------------------------------------------
SAUDI_KAYAN_SOURCE_REGISTRY: dict[tuple[str, int], dict] = {
    ("2350", 2018): {
        "source_url": "https://www.saudikayan.com/en/Images/Kayan-Report-en_tcm1043-19130.pdf",
        "source_website": "saudikayan.com", "source_tier": 1,
        "document_type": "annual_report", "source_type": "pdf",
    },
    ("2350", 2019): {
        "source_url": "https://www.saudikayan.com/en/Images/kayan-2019-report-ar_tcm1043-22275.pdf",
        "source_website": "saudikayan.com", "source_tier": 1,
        "document_type": "annual_report", "source_type": "pdf",
    },
    ("2350", 2020): {
        "source_url": "https://www.saudikayan.com/en/Images/Annual%202020_En_tcm1043-33650.pdf",
        "source_website": "saudikayan.com", "source_tier": 1,
        "document_type": "annual_report", "source_type": "pdf",
    },
    ("2350", 2021): {
        "source_url": "https://www.saudikayan.com/en/Images/Annual%202021_En_tcm1043-33651.pdf",
        "source_website": "saudikayan.com", "source_tier": 1,
        "document_type": "annual_report", "source_type": "pdf",
    },
    ("2350", 2022): {
        "source_url": "https://www.saudikayan.com/en/Images/Annual%20Report%202022%20En_tcm1043-42458.pdf",
        "source_website": "saudikayan.com", "source_tier": 1,
        "document_type": "annual_report", "source_type": "pdf",
    },
    ("2350", 2024): {
        "source_url": "https://www.saudikayan.com/en/Images/Annual%20Report%202024%20SK_tcm1043-46850.pdf",
        "source_website": "saudikayan.com", "source_tier": 1,
        "document_type": "annual_report", "source_type": "pdf",
    },
    # 2015, 2016, 2017, 2023: no verified saudikayan.com URL surfaced (the
    # 2023 report was only found via argaamplus.s3.amazonaws.com — Tier 3,
    # not registered as primary source per instruction) -> MISSING.
}

SIPCHEM_SOURCE_REGISTRY: dict[tuple[str, int], dict] = {
    ("2310", 2022): {
        "source_url": "https://www.sipchem.com/sites/default/files/annual-reports/28656b51-9878-49d3-8115-aff5f4d80e56.pdf",
        "source_website": "sipchem.com", "source_tier": 1,
        "document_type": "annual_report", "source_type": "pdf",
    },
    # sipchem.com/en/reports is an official archive INDEX page, not a
    # year-specific report page, so it was NOT registered as report_page
    # for any single year. No other year-specific PDF was verified.
}

TASNEE_SOURCE_REGISTRY: dict[tuple[str, int], dict] = {
    ("2060", 2015): {
        "source_url": "https://www.tasnee.com/media/h3pj5ion/tasnee_ar_english_2015.pdf",
        "source_website": "tasnee.com", "source_tier": 1,
        "document_type": "annual_report", "source_type": "pdf",
    },
    ("2060", 2016): {
        "source_url": "https://www.tasnee.com/media/tvrl3a4d/tasnee_ar_english_2016.pdf",
        "source_website": "tasnee.com", "source_tier": 1,
        "document_type": "annual_report", "source_type": "pdf",
    },
    # 2017-2024: tasnee.com/investor-relations (Annual Reports tab) is a
    # verified official listing page, but WebSearch results did not
    # confirm it resolves to distinct, year-specific sub-pages per fiscal
    # year — so it was not registered as report_page for any single year
    # to avoid misrepresenting a generic index. Left MISSING.
}

SIIG_SOURCE_REGISTRY: dict[tuple[str, int], dict] = {
    ("2250", 2024): {
        # SIIG's own domain (siig.com.sa) had no direct PDF in search
        # results; this is a Tier 2 source — the Saudi Exchange directly
        # hosting SIIG's official annual report filing.
        "source_url": "https://www.saudiexchange.sa/Resources/fsPdf/405_0_2025-03-27_16-33-03_En.pdf",
        "source_website": "saudiexchange.sa", "source_tier": 2,
        "document_type": "annual_report", "source_type": "pdf",
    },
    # 2015-2023: no verified source_url found on siig.com.sa or
    # saudiexchange.sa for these years -> MISSING.
}

NAMA_CHEMICALS_SOURCE_REGISTRY: dict[tuple[str, int], dict] = {
    ("2210", 2020): {
        "source_url": "https://www.nama.com.sa/wp-content/uploads/2023/06/Board-Report-2020.pdf",
        "source_website": "nama.com.sa", "source_tier": 1,
        "document_type": "annual_report", "source_type": "pdf",
    },
    ("2210", 2021): {
        "source_url": "https://nama.com.sa/wp-content/uploads/2023/06/Board-Report-2021.pdf",
        "source_website": "nama.com.sa", "source_tier": 1,
        "document_type": "annual_report", "source_type": "pdf",
    },
    ("2210", 2022): {
        "source_url": "https://nama.com.sa/wp-content/uploads/2023/06/Board-Report-2022.pdf",
        "source_website": "nama.com.sa", "source_tier": 1,
        "document_type": "annual_report", "source_type": "pdf",
    },
    # 2015-2019, 2023, 2024: no verified nama.com.sa URL surfaced -> MISSING.
}

ALUJAIN_SOURCE_REGISTRY: dict[tuple[str, int], dict] = {
    ("2170", 2015): {
        "source_url": "https://www.alujain.sa/uploads/pdf/20190325052551-Annual_Report_2015_English.pdf",
        "source_website": "alujain.sa", "source_tier": 1,
        "document_type": "annual_report", "source_type": "pdf",
    },
    ("2170", 2020): {
        # Search returned this as http:// — normalized to https:// only
        # (same host, same path; no content/URL invented) since this
        # project's registry convention requires https.
        "source_url": "https://alujain.sa/media/rvydh1pg/ar_2020_en.pdf",
        "source_website": "alujain.sa", "source_tier": 1,
        "document_type": "annual_report", "source_type": "pdf",
    },
    ("2170", 2024): {
        "source_url": "https://alujain.sa/media/dzmptyp3/ar-2024-en.pdf",
        "source_website": "alujain.sa", "source_tier": 1,
        "document_type": "annual_report", "source_type": "pdf",
    },
    # 2016-2019, 2021-2023: no verified alujain.sa URL surfaced -> MISSING.
}

SABIC_AGRI_NUTRIENTS_SOURCE_REGISTRY: dict[tuple[str, int], dict] = {
    # Deliberately EMPTY. sabic-agrinutrients.com results returned an
    # ambiguously-labeled "ER 2023" document (Earnings Release, not
    # confirmed to be the Annual Report) and general investor-relations/
    # reports index pages, but no PDF or page verifiably tied to a
    # specific fiscal year's ANNUAL REPORT — per the no-guessing rule nothing
    # was registered rather than assuming "ER" means annual report.
}

COMPANY_SOURCE_REGISTRIES: dict[str, dict[tuple[str, int], dict]] = {
    "sabic": SABIC_SOURCE_REGISTRY,
    "yansab": YANSAB_SOURCE_REGISTRY,
    "advanced_petrochemical": ADVANCED_PETROCHEMICAL_SOURCE_REGISTRY,
    "saudi_kayan": SAUDI_KAYAN_SOURCE_REGISTRY,
    "sipchem": SIPCHEM_SOURCE_REGISTRY,
    "tasnee": TASNEE_SOURCE_REGISTRY,
    "siig": SIIG_SOURCE_REGISTRY,
    "nama_chemicals": NAMA_CHEMICALS_SOURCE_REGISTRY,
    "alujain": ALUJAIN_SOURCE_REGISTRY,
    "sabic_agri_nutrients": SABIC_AGRI_NUTRIENTS_SOURCE_REGISTRY,
    # A new company is added here purely as configuration, e.g.:
    #   "some_company": SOME_COMPANY_SOURCE_REGISTRY,
    # where SOME_COMPANY_SOURCE_REGISTRY is defined the same way as
    # SABIC_SOURCE_REGISTRY above — real, verified sources only, no
    # guessed URLs. No change to acquire_one_report/acquire_reports/
    # describe_registry_coverage is ever required to add a company here.
}

# Ticker lookup for the whole target universe — used by tests and future
# report-URL discovery steps. Kept as a single source of truth alongside
# COMPANY_SOURCE_REGISTRIES rather than re-deriving tickers from registry
# keys (which would be empty/ambiguous for companies with no entries yet).
CONFIRMED_COMPANY_TICKERS: dict[str, str] = {
    "sabic": "2010",
    "yansab": "2290",
    "advanced_petrochemical": "2330",
    "saudi_kayan": "2350",
    "sipchem": "2310",
    "tasnee": "2060",
    "siig": "2250",
    "nama_chemicals": "2210",
    "alujain": "2170",
    "sabic_agri_nutrients": "2020",
}

# ----------------------------------------------------------------------------
# PENDING_TICKER_CONFIRMATION — now EMPTY. All 10 companies in this
# project's documented target universe (README.md "PLANNED" roadmap
# section) have a verified ticker and a (possibly empty) registry wired
# into COMPANY_SOURCE_REGISTRIES above. Kept as an empty dict (not deleted)
# so any future company added to the target universe without a verified
# ticker yet has an established place to be recorded, and so
# get_registry_for_company() keeps behaving identically for any such
# future entry (KeyError, never a silent empty fallback).
# ----------------------------------------------------------------------------
PENDING_TICKER_CONFIRMATION: dict[str, str] = {}


def get_registry_for_company(company_slug: str) -> dict[tuple[str, int], dict]:
    """Looks up a company's source registry by slug (e.g. "sabic"). Raises
    KeyError with a clear message if the company has no registered
    configuration — this is deliberate: acquisition must never silently
    fall back to an empty or wrong registry for an unrecognized company."""
    try:
        return COMPANY_SOURCE_REGISTRIES[company_slug]
    except KeyError:
        raise KeyError(
            f"no source registry configured for company_slug={company_slug!r} "
            f"— add it to COMPANY_SOURCE_REGISTRIES before calling acquisition "
            f"functions for this company"
        ) from None


PDF_MAGIC_BYTES = b"%PDF-"

VALID_SOURCE_TYPES = frozenset({"pdf", "report_page"})


@dataclass
class AcquiredDocumentMetadata:
    """Local-only metadata record for one acquired (or attempted) report.
    Mirrors what will eventually become a core.source_documents row, but
    this object itself is never written to any database — it only ever
    lives in the local JSON manifest."""
    ticker: str
    fiscal_year: int
    document_type: str
    source_url: Optional[str]
    source_website: Optional[str]
    source_tier: Optional[int]
    local_path: Optional[str]
    sha256: Optional[str]
    page_count: Optional[int]
    # 'AVAILABLE' (PDF actually downloaded+verified) | 'MISSING' (no
    # source registered at all — nothing was attempted) | 'FAILED' (a
    # source WAS registered and a download/verification WAS attempted, but
    # it failed — HTTP error, network error, or post-download integrity
    # failure; kept distinct from MISSING per explicit instruction) |
    # 'CONFLICT' (existing local file failed integrity) |
    # 'REPORT_PAGE_ONLY' (a verified official source page is known, but no
    # PDF has been identified/downloaded — source-known is kept strictly
    # separate from download-complete, per explicit instruction) |
    # 'SOURCE_DISCOVERED_BUT_FILE_UNAVAILABLE' (the official source itself
    # is verified/known — e.g. re-confirmed via WebSearch — but no tool
    # available in this session/environment can actually retrieve the
    # file's bytes from that host; distinct from FAILED in that no
    # download attempt is repeated once this is established — repeating
    # the same blocked request adds no new information).
    acquisition_status: str
    file_size_bytes: Optional[int] = None
    source_type: Optional[str] = None  # 'pdf' | 'report_page' | None
    acquired_at: Optional[str] = None  # ISO8601, set by caller (no auto now())
    report_period_end: Optional[str] = None  # 'YYYY-MM-DD' if known
    failure_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


# --- Integrity checks --------------------------------------------------------

def is_valid_pdf(path: Path) -> bool:
    """True only if the file exists, is non-empty, and starts with the PDF
    magic bytes — catches the common failure mode of an HTML error page
    (e.g. a 404/login-wall page) saved with a .pdf extension."""
    if not path.exists() or path.stat().st_size == 0:
        return False
    with open(path, "rb") as f:
        header = f.read(len(PDF_MAGIC_BYTES))
    return header == PDF_MAGIC_BYTES


def count_pdf_pages(path: Path) -> Optional[int]:
    """Best-effort page count. Tries pdfplumber first (this project's
    established PDF library, per requirements-ingestion.txt) if it's
    installed; falls back to a crude byte-level count of '/Type /Page'
    object markers, which is approximate but works with zero dependencies
    and is good enough to sanity-check "did we get a real multi-page report
    or a 1-page error stub". Returns None if the file isn't readable at all
    — never raises."""
    try:
        import pdfplumber  # optional dependency; may not be installed
        with pdfplumber.open(path) as pdf:
            return len(pdf.pages)
    except ImportError:
        pass
    except Exception:
        return None

    try:
        raw = path.read_bytes()
    except OSError:
        return None
    # Approximate fallback: count distinct page object markers. This can
    # over/under-count on some PDF producers' output — it's a sanity check,
    # not an authoritative page count. A real extraction pass should prefer
    # pdfplumber's actual count.
    matches = re.findall(rb"/Type\s*/Page(?!s)", raw)
    return len(matches) if matches else None


def verify_document_integrity(path: Path) -> tuple[bool, list[str]]:
    """Runs every acquisition-time integrity check. Returns (ok, errors).
    A document is only ever marked AVAILABLE by acquire_reports() when
    ok=True here."""
    errors: list[str] = []
    if not path.exists():
        errors.append("file does not exist")
        return False, errors
    if path.stat().st_size == 0:
        errors.append("file is empty (0 bytes)")
    if not is_valid_pdf(path):
        errors.append(
            "file does not start with the PDF magic bytes (%PDF-) — likely "
            "an HTML error/login page saved with a .pdf extension, not a "
            "real PDF"
        )
    return (len(errors) == 0), errors


# --- Manifest read/write ------------------------------------------------------
# Directory/manifest location is keyed by a human-readable "company_slug"
# (e.g. "sabic"), separate from `ticker` (e.g. "2010") which is stored in
# the metadata itself. This matches the requested data/raw/sabic/ layout
# rather than a numeric ticker-named directory.

def manifest_path_for(company_slug: str) -> Path:
    return RAW_DATA_ROOT / company_slug.lower() / "manifest.json"


def load_manifest(company_slug: str) -> dict:
    """Returns the existing manifest dict for a company, or an empty
    scaffold if none exists yet. Never raises for a missing file."""
    path = manifest_path_for(company_slug)
    if not path.exists():
        return {"company_slug": company_slug, "records": {}}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_manifest(company_slug: str, manifest: dict) -> Path:
    path = manifest_path_for(company_slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    return path


# --- Acquisition --------------------------------------------------------------

def acquire_one_report(
    ticker: str,
    fiscal_year: int,
    now_iso: str,
    registry: dict,
    out_root: Path = RAW_DATA_ROOT,
    company_slug: Optional[str] = None,
) -> AcquiredDocumentMetadata:
    """GENERIC — contains no company-specific logic or defaults. Attempts
    to acquire exactly one fiscal year's report for one company, driven
    entirely by whatever `registry` dict is passed in (see
    get_registry_for_company() to resolve one by company_slug, e.g.
    get_registry_for_company("sabic")). `now_iso` is passed in by the
    caller (not computed here with datetime.now(), so this function stays
    fully unit-testable and
    deterministic) — the caller (acquire_reports) supplies a real
    timestamp when actually run.

    `company_slug` controls the directory name under out_root (e.g.
    "sabic"); defaults to `ticker.lower()` if not given, so existing
    ticker-keyed callers/tests are unaffected.

    Never guesses a URL. If `(ticker, fiscal_year)` has no entry in
    `registry`, the result is MISSING with an explicit reason — no network
    call is attempted at all in that case.

    Idempotency: if a file already exists at the expected local path AND
    its SHA-256 matches what a fresh download would need to be re-verified
    against (i.e. the file already passes integrity checks), it is treated
    as already-acquired and NOT re-downloaded.
    """
    key = (ticker, fiscal_year)
    slug = (company_slug or ticker).lower()
    year_dir = out_root / slug / str(fiscal_year)
    expected_filename = f"{ticker}_{fiscal_year}_annual_report.pdf"
    local_path = year_dir / expected_filename

    if key not in registry:
        return AcquiredDocumentMetadata(
            ticker=ticker,
            fiscal_year=fiscal_year,
            document_type="annual_report",
            source_url=None,
            source_website=None,
            source_tier=None,
            local_path=None,
            sha256=None,
            page_count=None,
            acquisition_status="MISSING",
            acquired_at=now_iso,
            failure_reason=(
                "no verified official source URL is registered for this "
                "(ticker, fiscal_year) in the provided registry — per the "
                "no-guessing rule, no URL was invented and no request was "
                "attempted"
            ),
        )

    entry = registry[key]
    source_type = entry.get("source_type", "pdf")

    # A "report_page" source is a known official location, NOT an acquired
    # document. It must never be fetched as if it were a PDF, never hashed,
    # and must never become 'AVAILABLE' — download status stays strictly
    # separate from source-known status, per explicit instruction.
    if source_type == "report_page":
        return AcquiredDocumentMetadata(
            ticker=ticker,
            fiscal_year=fiscal_year,
            document_type=entry.get("document_type", "annual_report"),
            source_url=entry["source_url"],
            source_website=entry.get("source_website"),
            source_tier=entry.get("source_tier"),
            local_path=None,
            sha256=None,
            page_count=None,
            acquisition_status="REPORT_PAGE_ONLY",
            source_type="report_page",
            acquired_at=now_iso,
            failure_reason=(
                "official SABIC report-listing page is known and verified, "
                "but no direct PDF has been identified from it yet — no "
                "download attempted, no file acquired"
            ),
        )

    # Idempotency: already have a valid file on disk? Don't re-download.
    if local_path.exists():
        ok, errors = verify_document_integrity(local_path)
        if ok:
            return AcquiredDocumentMetadata(
                ticker=ticker,
                fiscal_year=fiscal_year,
                document_type=entry.get("document_type", "annual_report"),
                source_url=entry["source_url"],
                source_website=entry.get("source_website"),
                source_tier=entry.get("source_tier"),
                local_path=str(local_path.relative_to(out_root.parent.parent)),
                sha256=sha256_file(local_path),
                page_count=count_pdf_pages(local_path),
                acquisition_status="AVAILABLE",
                source_type="pdf",
                file_size_bytes=local_path.stat().st_size,
                acquired_at=now_iso,
                failure_reason=None,
            )
        # Existing file fails integrity — do NOT silently overwrite it.
        # Flag as CONFLICT for manual review, per the explicit instruction
        # not to silently replace a changed/bad file.
        return AcquiredDocumentMetadata(
            ticker=ticker,
            fiscal_year=fiscal_year,
            document_type=entry.get("document_type", "annual_report"),
            source_url=entry["source_url"],
            source_website=entry.get("source_website"),
            source_tier=entry.get("source_tier"),
            local_path=str(local_path.relative_to(out_root.parent.parent)),
            sha256=None,
            page_count=None,
            acquisition_status="CONFLICT",
            source_type="pdf",
            acquired_at=now_iso,
            failure_reason=(
                "an existing local file was found but failed integrity "
                "checks: " + "; ".join(errors) + " — left untouched, "
                "not overwritten; needs manual review"
            ),
        )

    # Need to actually fetch it.
    try:
        import requests  # already a project dependency (requirements.txt)
    except ImportError:
        return AcquiredDocumentMetadata(
            ticker=ticker, fiscal_year=fiscal_year,
            document_type=entry.get("document_type", "annual_report"),
            source_url=entry["source_url"], source_website=entry.get("source_website"),
            source_tier=entry.get("source_tier"), local_path=None, sha256=None,
            page_count=None, acquisition_status="FAILED", source_type="pdf",
            acquired_at=now_iso,
            failure_reason="the 'requests' package is not available in this environment",
        )

    try:
        resp = requests.get(entry["source_url"], timeout=30)
        resp.raise_for_status()
    except Exception as e:
        return AcquiredDocumentMetadata(
            ticker=ticker, fiscal_year=fiscal_year,
            document_type=entry.get("document_type", "annual_report"),
            source_url=entry["source_url"], source_website=entry.get("source_website"),
            source_tier=entry.get("source_tier"), local_path=None, sha256=None,
            page_count=None, acquisition_status="FAILED", source_type="pdf",
            acquired_at=now_iso,
            failure_reason=f"HTTP/network error during download: {e}",
        )

    year_dir.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(resp.content)
    file_size = local_path.stat().st_size

    ok, errors = verify_document_integrity(local_path)
    if ok:
        page_count = count_pdf_pages(local_path)
        if not page_count or page_count <= 0:
            ok = False
            errors = errors + [
                f"page count check failed: count_pdf_pages() returned "
                f"{page_count!r} (must be > 0)"
            ]

    if not ok:
        return AcquiredDocumentMetadata(
            ticker=ticker, fiscal_year=fiscal_year,
            document_type=entry.get("document_type", "annual_report"),
            source_url=entry["source_url"], source_website=entry.get("source_website"),
            source_tier=entry.get("source_tier"),
            local_path=str(local_path.relative_to(out_root.parent.parent)),
            sha256=None, page_count=None, acquisition_status="FAILED",
            source_type="pdf", file_size_bytes=file_size,
            acquired_at=now_iso,
            failure_reason="downloaded file failed integrity checks: " + "; ".join(errors),
        )

    return AcquiredDocumentMetadata(
        ticker=ticker,
        fiscal_year=fiscal_year,
        document_type=entry.get("document_type", "annual_report"),
        source_url=entry["source_url"],
        source_website=entry.get("source_website"),
        source_tier=entry.get("source_tier"),
        local_path=str(local_path.relative_to(out_root.parent.parent)),
        sha256=sha256_file(local_path),
        page_count=count_pdf_pages(local_path),
        acquisition_status="AVAILABLE",
        source_type="pdf",
        file_size_bytes=file_size,
        acquired_at=now_iso,
        failure_reason=None,
    )


def acquire_reports(
    ticker: str,
    fiscal_years: list[int],
    registry: dict,
    out_root: Path = RAW_DATA_ROOT,
    company_slug: Optional[str] = None,
) -> dict:
    """GENERIC — contains no company-specific logic or defaults. Runs
    acquire_one_report() for every requested fiscal year against whatever
    `registry` is passed in (see get_registry_for_company()), updates and
    saves the manifest, and returns it. This is the only function in this
    module that performs network I/O (and only when a year has a
    registered URL) and the only one that writes to data/raw/ — it never
    touches Neon, never runs SQL, never extracts a financial fact.

    `company_slug` controls the directory name (e.g. "sabic"); defaults to
    `ticker.lower()` if not given."""
    slug = (company_slug or ticker).lower()
    now_iso = datetime.now(timezone.utc).isoformat()
    manifest = load_manifest(slug)
    manifest["ticker"] = ticker
    manifest["company_slug"] = slug
    manifest.setdefault("records", {})

    for fy in fiscal_years:
        record = acquire_one_report(
            ticker, fy, now_iso, registry=registry, out_root=out_root, company_slug=slug,
        )
        manifest["records"][str(fy)] = record.to_dict()

    manifest["last_run_at"] = now_iso
    save_manifest(slug, manifest)
    return manifest


def render_coverage_report(manifest: dict) -> str:
    """Pure formatting function — turns a manifest dict into the plain-text
    coverage table requested for reporting (download-status oriented:
    what's actually been fetched and verified locally). No I/O."""
    lines = []
    for fy_str in sorted(manifest.get("records", {}), key=int):
        rec = manifest["records"][fy_str]
        status = "FOUND" if rec["acquisition_status"] == "AVAILABLE" else rec["acquisition_status"]
        lines.append(f"FY{fy_str}  {status}")
    return "\n".join(lines)


def describe_registry_coverage(fiscal_years: list[int], ticker: str, registry: dict) -> list[dict]:
    """GENERIC — contains no company-specific logic or defaults. Pure,
    offline inspection of whatever `registry` dict is passed in — does NOT
    run acquisition, does NOT touch the network or filesystem beyond
    reading the in-memory dict. Reports whether a verified SOURCE exists per year
    and what type it is, deliberately NOT claiming any PDF has been
    downloaded. Used for source-coverage reporting distinct from
    render_coverage_report()'s download-status reporting above."""
    results = []
    for fy in fiscal_years:
        entry = registry.get((ticker, fy))
        if entry is None:
            results.append({"fiscal_year": fy, "found": False, "source_type": None, "source_url": None})
        else:
            results.append({
                "fiscal_year": fy,
                "found": True,
                "source_type": entry.get("source_type", "pdf"),
                "source_url": entry["source_url"],
            })
    return results


# ============================================================================
# TODO (explicitly out of scope for this task):
#   - extract_facts(pdf_path) -> list[CandidateFinancialFact]
#   - register_source_document(metadata) -> str (document_id) — this is a
#     DATABASE write and must not be built/called until explicitly approved
#   - load_facts(facts: list[CandidateFinancialFact]) -> LoadResult
# None of the above are implemented here. Do not call this module expecting
# any of them to exist yet. acquire_reports() above is the only function in
# this module that performs I/O beyond local file/manifest reads and writes.
# ============================================================================
