-- ============================================================================
-- VERIFICATION NOTE (Phase 0 foundation hardening)
-- ============================================================================
-- This file was cross-checked against the LIVE Neon database via read-only
-- introspection (information_schema.columns, table_constraints, pg_indexes)
-- on this date. Every table, column, default, CHECK constraint, foreign key,
-- and index below was confirmed to match production exactly at that time.
--
-- Two things this file does NOT (and cannot) fully capture from a static
-- read of the live schema, documented here rather than silently omitted:
--   1. Whether any column comments exist in the live database — Postgres
--      COMMENT ON statements were not queried this pass. If comments exist
--      in production that aren't reproduced here, this is a known gap, not
--      a claim that no comments exist.
--   2. Row-level security policies, if any — not queried this pass. Given
--      this project's read-only-role work, RLS may become relevant later;
--      it was not in scope for this verification.
--
-- This file is the reproducibility source of truth for the schema going
-- forward: any future schema change should be made here first, then applied
-- to Neon, not the reverse.
-- ============================================================================

-- ============================================================================
-- Saudi Petrochemical Intelligence — Core Data Schema (PostgreSQL)
-- ============================================================================
-- Design principles enforced by this schema (from Phase 0 / 0.5 agreements):
--   1. Flexible line-item model — no fixed "revenue/COGS/gross_profit" columns
--      that assume every company (bank, insurer, petrochemical) has the same
--      shape. Concepts are rows, not columns.
--   2. Raw vs normalized separation — raw extracted text is never overwritten,
--      normalized values live in a separate, derived layer.
--   3. Every number carries full provenance: source document, page, hash,
--      extraction method, confidence, and — critically — is_restated status.
--   4. Point-in-time discipline — publication_date is a first-class column,
--      distinct from fiscal period end, precisely because Phase 0 found this
--      is the #1 way naive backtests cheat with hindsight.
--   5. Nothing here computes anything. This is storage only. Metrics
--      (growth, margins, ratios) are a separate, deterministic compute layer
--      that reads FROM this schema — never written back into it as if it
--      were reported fact.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS core;
SET search_path TO core;

-- ----------------------------------------------------------------------------
-- 1. companies — master entity list. One row per legal/listed entity.
--    Historical entities (delisted/merged) stay here forever; they never
--    disappear from the universe (see historical_universe below).
--
--    Verified against live database evidence (information_schema.columns +
--    pg_constraint), manually queried by the project owner via Neon SQL
--    Editor on 2026-09-01 — this session did not independently verify the
--    raw query output, only received it pasted into the conversation and
--    cross-checked it against this file, column-by-column and constraint-
--    by-constraint. Result: all 12 columns and all 9 constraints below
--    already matched the live evidence exactly — no correction was needed
--    here (a prior report of a "company_name" column mismatch was not
--    reproducible against this file; no such column reference exists
--    anywhere in this file or in app.py/us_xbrl_api.py).
-- ----------------------------------------------------------------------------
CREATE TABLE companies (
    company_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticker           VARCHAR(10),           -- current ticker, NULL if delisted with no successor mapping needed
    name_ar          TEXT NOT NULL,
    name_en          TEXT NOT NULL,
    sector           TEXT,                  -- e.g. 'Materials', 'PharmaBiotech & Life Science' (SAHMK-style taxonomy)
    industry_group   TEXT,                  -- our own classification, e.g. 'Petrochemicals - Olefins/Polymers'
    incorporated_year SMALLINT,
    status           TEXT NOT NULL CHECK (status IN ('active','delisted','merged','renamed','acquired','suspended')),
    parent_company_id UUID REFERENCES companies(company_id), -- e.g. YANSAB -> SABIC
    website          TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_companies_ticker ON companies(ticker) WHERE status = 'active';

-- ----------------------------------------------------------------------------
-- 2. historical_universe — survivorship-bias control (Phase 0, item F).
--    Tracks every name/ticker/status change over time so a backtest as-of
--    2019 sees the universe as it actually was in 2019, not today's survivors.
-- ----------------------------------------------------------------------------
CREATE TABLE historical_universe (
    record_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id        UUID NOT NULL REFERENCES companies(company_id),
    historical_name   TEXT NOT NULL,
    historical_ticker VARCHAR(10),
    event_type        TEXT NOT NULL CHECK (event_type IN
                        ('listed','delisted','merged','renamed','acquired','suspended','privatized')),
    effective_from    DATE NOT NULL,
    effective_to      DATE,                 -- NULL = still in effect
    successor_company_id UUID REFERENCES companies(company_id),
    source            TEXT NOT NULL,        -- how we know this (news article, Tadawul announcement, etc.)
    source_url        TEXT,
    confidence        TEXT NOT NULL CHECK (confidence IN ('HIGH','MEDIUM','LOW')),
    notes             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- 3. source_documents — the raw filing/announcement itself, hashed.
--    One row per PDF/HTML disclosure we've ingested. Never mutated after
--    insert (append-only); if a document is superseded, insert a new row
--    and link via supersedes_document_id.
-- ----------------------------------------------------------------------------
CREATE TABLE source_documents (
    document_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id         UUID NOT NULL REFERENCES companies(company_id),
    document_type      TEXT NOT NULL CHECK (document_type IN
                         ('annual_report','quarterly_report','disclosure_announcement',
                          'earnings_presentation','prospectus','other')),
    fiscal_year         SMALLINT,
    fiscal_quarter       SMALLINT CHECK (fiscal_quarter BETWEEN 1 AND 4),
    report_period_end   DATE,               -- the fiscal period this document REPORTS ON
    publication_date    DATE,               -- when it was actually PUBLISHED — NOT the same as period_end
    publication_time    TIME,               -- populated when source provides it (Saudi Exchange disclosures do)
    source_url          TEXT NOT NULL,
    source_website       TEXT NOT NULL,      -- e.g. 'yansab.com.sa', 'saudiexchange.sa'
    source_tier          SMALLINT NOT NULL CHECK (source_tier BETWEEN 1 AND 4), -- 1=official filing, per Phase 0 hierarchy
    download_timestamp    TIMESTAMPTZ NOT NULL DEFAULT now(),
    document_sha256        CHAR(64) NOT NULL,
    page_count              INT,
    supersedes_document_id  UUID REFERENCES source_documents(document_id),
    availability_status      TEXT NOT NULL DEFAULT 'AVAILABLE'
                              CHECK (availability_status IN ('AVAILABLE','MISSING','ACCESS_BLOCKED')),
    UNIQUE (document_sha256)
);
CREATE INDEX idx_source_documents_company_period ON source_documents(company_id, fiscal_year, fiscal_quarter);

-- ----------------------------------------------------------------------------
-- 4. financial_line_items — THE flexible model. Every extracted number is a
--    row here, not a column in a fixed table. This is what lets a bank, an
--    insurer, and a petrochemical company coexist without schema surgery.
--
--    Corrected against live database evidence (information_schema.columns +
--    pg_constraint), manually queried by the project owner via Neon SQL
--    Editor on 2026-09-01 — this session did not independently verify the
--    raw query output, only received it pasted into the conversation and
--    cross-checked it against this file. period_type/fiscal_half below were
--    previously documented only as an unintegrated drift note further down
--    this file (see "KNOWN LIVE-SCHEMA DRIFT"), with their CHECK domains
--    unknown; this pass's raw pg_constraint output gave the actual CHECK
--    definitions, so they are now added directly to this CREATE TABLE.
-- ----------------------------------------------------------------------------
CREATE TABLE financial_line_items (
    line_item_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id          UUID NOT NULL REFERENCES companies(company_id),
    document_id          UUID NOT NULL REFERENCES source_documents(document_id),
    statement_type        TEXT NOT NULL CHECK (statement_type IN
                            ('income_statement','balance_sheet','cash_flow','equity_changes','segment','other')),
    concept                TEXT NOT NULL,   -- normalized concept key, e.g. 'revenue', 'net_income', 'eps_basic'
                                             -- (mapped via concept_dictionary below — NOT a hardcoded enum,
                                             -- so sector-specific concepts like 'net_interest_margin' for banks
                                             -- or 'net_earned_premium' for insurers can be added without a migration)
    reported_label         TEXT NOT NULL,   -- the exact label as printed in the filing, e.g.
                                             -- '(Losses) earnings Per Share (SR)'
    fiscal_year             SMALLINT NOT NULL,
    fiscal_quarter            SMALLINT CHECK (fiscal_quarter BETWEEN 1 AND 4), -- NULL = annual
    period_start              DATE,
    period_end                DATE NOT NULL,
    value_raw                  NUMERIC(20,4) NOT NULL,   -- exactly as extracted, unit-tagged separately below
    currency                    CHAR(3) NOT NULL DEFAULT 'SAR',
    unit                         TEXT NOT NULL CHECK (unit IN ('unit','thousand','million')),
    is_restated                   BOOLEAN,       -- NULL = unknown/not checked, not FALSE-by-default (Phase 0.5 rule:
                                                  -- never assume "not restated" just because we didn't check)
    restated_from_line_item_id     UUID REFERENCES financial_line_items(line_item_id),
    source_page                    INT,
    extraction_method                TEXT NOT NULL,  -- 'pdfplumber_text_keyword_v2', 'manual_verification', etc.
    confidence                        TEXT NOT NULL CHECK (confidence IN ('HIGH','MEDIUM','LOW')),
    raw_text                          TEXT,           -- the full extracted line/window, for human audit
    extracted_at                       TIMESTAMPTZ NOT NULL DEFAULT now(),
    parser_version                      TEXT,
    reviewed_by_human                    BOOLEAN NOT NULL DEFAULT FALSE,
    review_notes                          TEXT,
    period_type                            TEXT NOT NULL CHECK (period_type IN
                                            ('FY','Q1','Q2','Q3','Q4','H1','H2')),
    fiscal_half                             SMALLINT CHECK (fiscal_half IN (1,2))
);
CREATE INDEX idx_fli_company_concept_period ON financial_line_items(company_id, concept, fiscal_year, fiscal_quarter);
CREATE INDEX idx_fli_confidence ON financial_line_items(confidence) WHERE confidence = 'LOW';
-- LOW confidence rows are queryable-but-flagged, never silently dropped, per
-- Phase 0.5 rule #13: "any LOW number does not enter the production dataset
-- automatically" — enforced at the application layer via a view (below), not
-- by deleting the row (we keep it for audit trail).

CREATE OR REPLACE VIEW production_financial_line_items AS
    SELECT * FROM financial_line_items WHERE confidence IN ('HIGH','MEDIUM');

-- ----------------------------------------------------------------------------
-- 5. concept_dictionary — controlled vocabulary + sector applicability.
--    This is what makes the model "flexible" in practice: adding a new
--    concept (e.g. a bank's 'net_interest_margin') is an INSERT, not a
--    migration.
-- ----------------------------------------------------------------------------
CREATE TABLE concept_dictionary (
    concept_key       TEXT PRIMARY KEY,          -- 'revenue', 'net_income', 'net_interest_margin', ...
    display_name_en    TEXT NOT NULL,
    display_name_ar     TEXT,
    statement_type        TEXT NOT NULL,
    applicable_sectors      TEXT[],              -- NULL/empty = universal; else e.g. ARRAY['Banks']
    is_computed               BOOLEAN NOT NULL DEFAULT FALSE, -- TRUE for e.g. 'ebitda' if we ever derive it
                                                                -- ourselves rather than take it as reported —
                                                                -- and if TRUE it must live in derived_metrics,
                                                                -- never in financial_line_items (Phase 0.5 rule:
                                                                -- never label a computed number as if reported)
    notes                      TEXT
);

-- ----------------------------------------------------------------------------
-- 6. market_prices — daily OHLCV, adjusted vs unadjusted kept as separate
--    explicit columns (never silently overwrite raw close with adjusted).
-- ----------------------------------------------------------------------------
CREATE TABLE market_prices (
    price_id          BIGSERIAL PRIMARY KEY,
    company_id          UUID NOT NULL REFERENCES companies(company_id),
    trade_date            DATE NOT NULL,
    open_price               NUMERIC(12,4),
    high_price                 NUMERIC(12,4),
    low_price                    NUMERIC(12,4),
    close_price                    NUMERIC(12,4) NOT NULL,
    adjusted_close_price              NUMERIC(12,4),   -- NULL until we've actually verified an adjustment method;
                                                        -- never defaulted to equal close_price silently
                                                        -- (SAHMK's own docs admit their adjusted_close often
                                                        -- just mirrors close post-March-2024 — we do not repeat
                                                        -- that ambiguity in our own storage layer)
    volume                              BIGINT,
    traded_value                          NUMERIC(18,2),
    is_delayed                              BOOLEAN,
    source                                    TEXT NOT NULL,
    source_id                                  UUID REFERENCES source_documents(document_id),
    retrieved_at                                TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company_id, trade_date, source)
);
CREATE INDEX idx_market_prices_company_date ON market_prices(company_id, trade_date);

-- ----------------------------------------------------------------------------
-- 7. corporate_actions — bonus shares, splits, rights issues, mergers.
--    Every event here should, in principle, be checkable against a documented
--    adjustment in market_prices; we don't assume that link exists until
--    verified (see validation_log below).
-- ----------------------------------------------------------------------------
CREATE TABLE corporate_actions (
    action_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id          UUID NOT NULL REFERENCES companies(company_id),
    action_type            TEXT NOT NULL CHECK (action_type IN
                             ('bonus_shares','stock_split','reverse_split','rights_issue',
                              'capital_increase','capital_decrease','dividend','merger','acquisition')),
    announcement_date          DATE,
    effective_date                DATE NOT NULL,
    ratio_description                TEXT,    -- human-readable, e.g. '1 bonus share per 5 held'
    ratio_numeric                       NUMERIC(10,6), -- e.g. 1.20 for a 1:5 bonus (20% increase)
    old_shares_outstanding                  BIGINT,
    new_shares_outstanding                     BIGINT,
    source_document_id                           UUID REFERENCES source_documents(document_id),
    source_page                                    INT,
    price_adjustment_verified                        BOOLEAN NOT NULL DEFAULT FALSE, -- flips TRUE only after
                                                                                       -- we've actually checked
                                                                                       -- market_prices reflects it
    notes                                             TEXT
);

-- ----------------------------------------------------------------------------
-- 8. derived_metrics — the ONLY place computed numbers live (growth, margins,
--    ratios). Strictly separate from financial_line_items (reported facts).
--    This is the enforcement point for the "computation/LLM separation"
--    architectural rule from the original master prompt.
-- ----------------------------------------------------------------------------
CREATE TABLE derived_metrics (
    metric_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id           UUID NOT NULL REFERENCES companies(company_id),
    metric_key             TEXT NOT NULL,     -- 'revenue_growth_yoy', 'ebitda_margin', 'net_debt_to_ebitda', ...
    fiscal_year               SMALLINT NOT NULL,
    fiscal_quarter               SMALLINT,
    value                           NUMERIC(20,6) NOT NULL,
    computed_from_line_item_ids        UUID[] NOT NULL, -- full lineage back to financial_line_items rows used
    computation_method                    TEXT NOT NULL, -- e.g. 'code:metrics_engine.py:revenue_growth_yoy:v1'
    computed_at                              TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_estimate                                 BOOLEAN NOT NULL DEFAULT FALSE  -- TRUE if any input was itself
                                                                                 -- an ANALYST ESTIMATE, not fact
);

-- ----------------------------------------------------------------------------
-- 9. validation_log — every cross-check we've ever run (official vs SAHMK vs
--    parser vs prior-year-comparative), append-only audit trail.
-- ----------------------------------------------------------------------------
CREATE TABLE validation_log (
    validation_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id             UUID NOT NULL REFERENCES companies(company_id),
    concept                   TEXT NOT NULL,
    fiscal_year                  SMALLINT NOT NULL,
    fiscal_quarter                  SMALLINT,
    source_a_id                        UUID REFERENCES source_documents(document_id),
    source_a_value                        NUMERIC(20,4),
    source_b_id                              UUID REFERENCES source_documents(document_id),
    source_b_value                              NUMERIC(20,4),
    difference_absolute                            NUMERIC(20,4),
    difference_percent                                NUMERIC(10,4),
    match_status                                        TEXT NOT NULL CHECK (match_status IN
                                                          ('EXACT_MATCH','MATCH_WITHIN_TOLERANCE','MISMATCH',
                                                           'AMBIGUOUS','MISSING_ONE_SIDE')),
    mismatch_reason                                        TEXT CHECK (mismatch_reason IS NULL OR mismatch_reason IN
                                                             ('RESTATEMENT','ROUNDING','CURRENCY','UNIT',
                                                              'CONSOLIDATION','PERIOD','UNKNOWN')),
    validated_at                                              TIMESTAMPTZ NOT NULL DEFAULT now(),
    validated_by                                                 TEXT NOT NULL  -- 'manual' or a script identifier
);

-- ----------------------------------------------------------------------------
-- 10. data_gaps — first-class tracking of what we DON'T have, so gaps are
--     queryable, not just prose in a report that goes stale.
-- ----------------------------------------------------------------------------
CREATE TABLE data_gaps (
    gap_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id            UUID REFERENCES companies(company_id),
    gap_type                 TEXT NOT NULL CHECK (gap_type IN
                              ('MISSING_PERIOD','MISSING_CONCEPT','SOURCE_UNAVAILABLE',
                               'ACCESS_BLOCKED','UNVERIFIED_ADJUSTMENT','LICENSING_RESTRICTED')),
    description                  TEXT NOT NULL,
    identified_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved                            BOOLEAN NOT NULL DEFAULT FALSE,
    resolved_at                            TIMESTAMPTZ
);

-- ============================================================================
-- Seed: concept_dictionary starter set (the concepts our v2 parser already
-- extracts — deliberately not exhaustive, grows via INSERT not migration)
-- ============================================================================
INSERT INTO concept_dictionary (concept_key, display_name_en, statement_type, applicable_sectors) VALUES
    ('revenue', 'Revenue', 'income_statement', NULL),
    ('gross_profit', 'Gross Profit', 'income_statement', NULL),
    ('operating_income', 'Operating Income', 'income_statement', NULL),
    ('net_income', 'Net Income (Loss) for the Year', 'income_statement', NULL),
    ('eps_basic', 'Basic Earnings (Loss) Per Share', 'income_statement', NULL),
    ('total_assets', 'Total Assets', 'balance_sheet', NULL),
    ('total_equity', 'Total Equity', 'balance_sheet', NULL),
    ('total_liabilities', 'Total Liabilities', 'balance_sheet', NULL),
    ('cash_and_equivalents', 'Cash and Cash Equivalents', 'balance_sheet', NULL),
    ('cfo', 'Net Cash from Operating Activities', 'cash_flow', NULL),
    ('capex', 'Purchase of Property, Plant and Equipment', 'cash_flow', NULL);

-- ============================================================================
-- KNOWN LIVE-SCHEMA DRIFT (documented as of Phase 1 preparation)
-- ============================================================================
-- IMPORTANT — READ BEFORE RUNNING THIS FILE FRESH:
--
-- Everything above this point is the original "fresh install" script and
-- matches what a brand-new database would get if this file were run
-- top-to-bottom against an empty Postgres instance.
--
-- Everything in this section is DIFFERENT: it documents changes that
-- ALREADY EXIST on the live Neon database (confirmed via direct,
-- individually-run SELECT / information_schema queries during Phase 1
-- preparation) but were made directly against Neon outside of this repo's
-- history, and have NEVER been executed as part of this file or any
-- committed migration.
--
-- THE SQL BELOW HAS NOT BEEN RUN BY THIS TASK. It is a documentation-only
-- reference so that (a) a fresh install can be made to match production if
-- someone chooses to run it, and (b) the first real migration files in
-- migrations/ have a precise, evidence-based starting point instead of
-- guessing at column types/nullability. Running this file fresh today
-- WITHOUT the statements below would NOT reproduce the current live
-- schema — that gap is exactly what this section exists to make visible.
--
-- Do not assume this list is exhaustive. It reflects only what has been
-- directly confirmed via live query results as of this preparation pass.
-- ============================================================================

-- --- financial_line_items: two live columns with no committed origin -------
-- Confirmed via a direct query for period_type/fiscal_half values:
--   period_type: 62/62 rows = 'FY', spanning fiscal_year 2022-2024
--   fiscal_half: 62/62 rows = NULL
-- Only ONE value ('FY') and one NULL state have ever been observed. This is
-- NOT sufficient evidence to declare the full set of valid period_type
-- values (e.g. whether 'Q' or 'H' values are intended/used elsewhere) —
-- treat the column's full domain as unknown beyond what's been observed.
--
-- ALTER TABLE financial_line_items ADD COLUMN period_type TEXT NOT NULL;
-- ALTER TABLE financial_line_items ADD COLUMN fiscal_half SMALLINT NULL;
--
-- SUPERSEDED (2026-09-01): the two ALTER statements above are no longer the
-- current state of this file — period_type and fiscal_half have now been
-- added directly to the financial_line_items CREATE TABLE above, WITH their
-- actual CHECK domains, discovered via a fresh raw pg_constraint pull this
-- pass (previously unknown beyond the single observed 'FY'/NULL values
-- noted above, which is left in place as the historical record of what was
-- known at the time):
--   financial_line_items_period_type_check: CHECK (period_type IN
--     ('FY','Q1','Q2','Q3','Q4','H1','H2'))
--   financial_line_items_fiscal_half_check: CHECK (fiscal_half IN (1,2))
-- This session did not independently verify the raw query output — it was
-- manually queried by the project owner via Neon SQL Editor and pasted into
-- the conversation, then cross-checked against this file.

-- --- canonical_concept_mapping: entire table with no committed origin ------
-- Confirmed live structure (via information_schema.columns), 3 rows exist,
-- all status='PROVISIONAL', all is_active=true. FK-linked to
-- concept_dictionary(concept_key) via concept_key.
--
-- CREATE TABLE canonical_concept_mapping (
--     mapping_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
--     canonical_key  TEXT NOT NULL,
--     concept_key    TEXT NOT NULL REFERENCES concept_dictionary(concept_key),
--     rationale      TEXT NOT NULL,
--     status         TEXT NOT NULL DEFAULT 'PROVISIONAL',
--     is_active      BOOLEAN NOT NULL DEFAULT true,
--     created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
--     updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
-- );
--
-- Current live rows (as of Phase 1 preparation — DO NOT re-insert, re-seed,
-- modify, or promote any of these; they are documented here for reference
-- only, exactly as confirmed live, and must be left untouched):
--   canonical_eps         -> eps_basic_total                         (PROVISIONAL)
--   canonical_net_income  -> net_income_attributable_to_parent       (PROVISIONAL)
--   canonical_equity      -> equity_attributable_to_parent           (PROVISIONAL)

-- --- concept_dictionary: live content vs this file's seed ------------------
-- This file's seed above inserts 11 concepts. The live table currently
-- contains 19 (confirmed via direct query): all 11 seeded here are present,
-- plus 8 more added directly against Neon outside this repo's history:
--   cost_of_revenue, eps_basic_continuing, eps_basic_total,
--   equity_attributable_to_parent, net_income_attributable_to_parent,
--   net_income_continuing, net_income_continuing_attributable_to_parent,
--   net_income_total
-- No INSERT is added here for these 8 — this is a documentation note, not
-- an executed or proposed migration, pending a future dedicated migration
-- file in migrations/ once a human has reviewed and confirmed each one's
-- exact display_name_ar/notes/is_computed values from Neon directly.
--
-- Verified (as of Phase 1 preparation): no financial_line_items row uses a
-- concept absent from this live 19-concept set, and no statement_type
-- mismatch exists between financial_line_items and concept_dictionary for
-- any concept in use.

-- --- financial_line_items: actual row count and concept usage (2026-09-01) -
-- A later, separate confirmation pass (manually queried by the project
-- owner via Neon SQL Editor, pasted into the conversation, cross-checked
-- but not independently re-run by this session) found:
--   - financial_line_items total row count = 62, fiscal_year range 2022-2024
--     (this matches the period_type/fiscal_half row count noted above — same
--     62-row live state, now corroborated from a second angle).
--   - source_documents count = 4.
--   - Row distribution by company: SABIC (ticker 2010) = 14 concepts x 3
--     fiscal years (2022, 2023, 2024) = 42 rows; YANSAB (ticker 2290) = 8
--     concepts x 2 fiscal years (2023, 2024) = 16 rows; Advanced
--     Petrochemical (ticker 2330) = 2 concepts x 2 fiscal years (2023, 2024)
--     = 4 rows. 42 + 16 + 4 = 62, internally consistent with the total above.
--     Coverage is uneven across the three companies with any data loaded —
--     this is additional detail beyond, and does not contradict, this
--     file's earlier documented gap that only 2-3 of the ~10 target
--     companies have any financial data at all.
--   - Distinct concepts actually IN USE in financial_line_items = 16 (NOT
--     11 — this file's seed count — and NOT 19 — the concept_dictionary
--     live-table count documented immediately above; 16 is a third,
--     separate figure: the subset of the 19 live dictionary concepts that
--     actually appears on at least one financial_line_items row):
--     cost_of_revenue, eps_basic, eps_basic_continuing, eps_basic_total,
--     equity_attributable_to_parent, gross_profit, net_income,
--     net_income_attributable_to_parent, net_income_continuing,
--     net_income_continuing_attributable_to_parent, net_income_total,
--     operating_income, revenue, total_assets, total_equity,
--     total_liabilities.
--   - This 16-in-use figure is consistent with, not contradictory to, the
--     19-concept live concept_dictionary count above: concept_dictionary is
--     the controlled vocabulary (what CAN be used), financial_line_items is
--     actual usage (what HAS been used) — 3 of the 19 live concepts
--     (capex, cash_and_equivalents, cfo) are dictionary entries with zero
--     rows using them yet, exactly as already noted in README.md's "Known
--     Limitations" (no cash-flow data loaded yet).
