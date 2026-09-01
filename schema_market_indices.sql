-- ============================================================================
-- schema_market_indices.sql
--
-- PROPOSED NEW TABLE — NOT YET EXECUTED AGAINST NEON.
--
-- This file defines core.market_indices, a small table to hold market-index
-- (e.g. TASI) daily closing values, needed to compute Abnormal Return
-- (Stock Return - Index Return) for the companies already in
-- core.market_prices. It does NOT reuse core.market_prices directly because
-- that table's company_id column has a mandatory FK to core.companies, and
-- a market index is not a company — inserting a synthetic "TASI" row into
-- core.companies to satisfy that FK would pollute a real-companies table
-- with a non-company entity. This is a separate, small, purpose-built table
-- instead.
--
-- STATUS: this SQL has NOT been run against the live Neon database by this
-- session. It is meant to be reviewed and executed manually (e.g. via the
-- Neon SQL Editor / Render Shell), the same way every other database change
-- in this project's history has been applied.
-- ============================================================================

CREATE TABLE IF NOT EXISTS core.market_indices (
    index_id      BIGSERIAL PRIMARY KEY,
    index_code    TEXT NOT NULL,          -- e.g. 'TASI' — literal, not a foreign key to anything
    trade_date    DATE NOT NULL,
    close_value   NUMERIC NOT NULL,
    source        TEXT NOT NULL,          -- e.g. 'yahoo_finance' — see scripts/fetch_market_index.py
    retrieved_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT market_indices_index_code_trade_date_source_key
        UNIQUE (index_code, trade_date, source)
);
