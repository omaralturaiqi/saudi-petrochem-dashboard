-- ============================================================================
-- schema_us_xbrl.sql
--
-- DOCUMENTATION OF EXISTING LIVE STRUCTURE — NOT A NEW DESIGN.
--
-- This file documents the live us_xbrl.canonical_financial_records structure
-- based on raw information_schema.columns and pg_constraint output pasted
-- directly into this session by the project owner (reflecting a manual Neon
-- SQL Editor query he ran, since no automated Neon connection was available
-- in this session). The column list and constraints below are transcribed
-- directly from that pasted output, which is included in this commit's
-- history/PR description for independent verification. The pg_indexes query
-- for this schema did not yield distinguishable results in this session; no
-- index information beyond what PostgreSQL automatically creates for the
-- PRIMARY KEY and UNIQUE constraints below is asserted.
--
-- Context: this schema had no CREATE SCHEMA / CREATE TABLE statement
-- committed anywhere in the repository prior to this file. It existed only
-- as a live, undocumented structure in Neon, referenced solely through the
-- column list used in us_xbrl_api.py's SQL queries. This file closes that
-- documentation gap so the schema can be reconstructed from source control
-- if the live database were ever lost.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS us_xbrl;

CREATE TABLE IF NOT EXISTS us_xbrl.canonical_financial_records (
    record_id        uuid                     NOT NULL DEFAULT gen_random_uuid(),
    company          text                     NOT NULL,
    ticker           text                     NOT NULL,
    cik              integer                  NOT NULL,
    metric           text                     NOT NULL,
    value            numeric,
    unit             text,
    period_start     date,
    period_end       date,
    fiscal_year      integer,
    concept          text,
    source           text                     NOT NULL,
    filing           text,
    form             text,
    confidence       text                     NOT NULL,
    status           text                     NOT NULL,
    unit_confirmed   boolean                  NOT NULL,
    period_valid     boolean                  NOT NULL,
    concept_status   text,
    entity_status    text,
    source_filing    text,
    created_at       timestamp with time zone NOT NULL DEFAULT now(),

    CONSTRAINT canonical_financial_records_pkey
        PRIMARY KEY (record_id),

    CONSTRAINT canonical_financial_records_ticker_metric_fiscal_year_key
        UNIQUE (ticker, metric, fiscal_year)
);
