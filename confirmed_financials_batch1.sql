-- confirmed_financials_batch1.sql
--
-- NOT EXECUTED against Neon by any script or session — review before running
-- manually (Neon SQL Editor / Render Shell), same as every other database
-- write in this project's history.
--
-- SCOPE OF THIS BATCH: ALBABTAIN (2320) ONLY. EIC (1303) and ABO MOATI
-- (4191) are intentionally EXCLUDED from this batch — the project owner
-- has only a secondary-source citation (Mubasher, itself citing a Tadawul
-- announcement) for EIC and no personally-verified primary-source URL for
-- ABO MOATI yet. Inserting either now would mean fabricating a
-- source_documents row for a document nobody has actually looked at here.
-- Pending separate verification before insertion — see the task's own
-- final report for this explicit deferral.
--
-- DOCUMENT_ID / source_documents — WHY THIS BATCH ALSO INSERTS ONE:
-- core.financial_line_items.document_id is NOT NULL and REFERENCES
-- source_documents(document_id) (verified against schema.sql this
-- session). No source_documents row already exists for this Tadawul
-- announcement (it is not a PDF this project has fetched/hashed before),
-- so one is created here, in the SAME file, with a literal UUID
-- ('e335e239-6dd9-4865-be72-072733fcf652') reused across both INSERTs
-- below — NOT gen_random_uuid() in each statement separately, which
-- would produce two different, unrelated UUIDs and break the FK link
-- between them.
--
-- DOCUMENT_SHA256 — READ THIS BEFORE TRUSTING IT AS A DOCUMENT HASH:
-- This SHA-256 is of EXTRACTED/VERIFIED TEXT CONTENT (the Arabic official
-- disclosure table's figures, attribution, and the announcement's own
-- anId, read and transcribed verbatim by the project owner via their own
-- web_fetch tool against saudiexchange.sa) — it is NOT a hash of the raw
-- HTML bytes of that page. A raw fetch (curl) of saudiexchange.sa was
-- blocked (Access Denied / WAF) from every execution environment
-- available this session — Render Shell, and the project owner's own
-- bash tool — confirming this is a genuine, structural access
-- limitation, not a one-off failure. Hashing the verified/transcribed
-- text is the closest honest verification achievable given that
-- constraint, and is documented as such here rather than presented as
-- if it were a hash of the original document bytes.
--
-- UNIT CONVENTION — value_raw=453000, unit='thousand' (NOT '453'/
-- 'million', NOT 453000000/full-SAR-units, both of which were floated as
-- alternatives): parser.py's CONCEPTS (the actual code that produced the
-- 62 already-live financial_line_items rows) hardcodes unit='thousand'
-- for every extracted value — confirmed by reading parser.py directly
-- this session, not assumed. 453000 (thousand SAR) = SAR 453 million,
-- matching the originally-stated figure and the established live
-- convention, chosen over the two guessed alternatives for consistency,
-- per this task's own "للتناسق" instruction.
--
-- YoY change (+70.49% vs FY2024, per the source citation this batch is
-- built from) is recorded as a SQL comment only (see below the INSERT),
-- never as a new column — per this task's own explicit instruction.

-- ============================================================================
-- 1. source_documents row for the ALBABTAIN FY2025 Tadawul announcement.
-- ============================================================================
INSERT INTO core.source_documents (
    document_id, company_id, document_type, fiscal_year, fiscal_quarter,
    report_period_end, publication_date, publication_time,
    source_url, source_website, source_tier, download_timestamp,
    document_sha256, page_count, supersedes_document_id, availability_status
)
SELECT
    'e335e239-6dd9-4865-be72-072733fcf652',
    (SELECT company_id FROM core.companies WHERE ticker = '2320'),
    'disclosure_announcement',
    2025,
    NULL,
    '2025-12-31',
    NULL,  -- publication_date: not captured from the citation, not guessed
    NULL,  -- publication_time: not captured, not guessed
    'https://www.saudiexchange.sa/wps/portal/saudiexchange/newsandreports/issuer-news/issuer-announcements/issuer-announcements-details/?anCat=1&anId=93565&cs=2320&locale=ar',
    'saudiexchange.sa',
    1,     -- source_tier 1: official Tadawul (Saudi Exchange) announcement
    now(),
    'e20aed83aaa9c41d8584f3bec2113b8b5f0af8ecaf2815bd803ed44c4a51dda4',
    NULL,  -- page_count: not applicable to a web disclosure page
    NULL,
    'AVAILABLE'
WHERE NOT EXISTS (
    SELECT 1 FROM core.source_documents WHERE document_sha256 = 'e20aed83aaa9c41d8584f3bec2113b8b5f0af8ecaf2815bd803ed44c4a51dda4'
);

-- ============================================================================
-- 2. financial_line_items row: ALBABTAIN (2320) FY2025 net income
--    attributable to parent, SAR 453,000 thousand (= SAR 453 million).
--    YoY vs FY2024: +70.49% (per the source citation — recorded here as a
--    comment only, never as a column, per this task's explicit instruction).
-- ============================================================================
INSERT INTO core.financial_line_items (
    line_item_id, company_id, document_id, statement_type, concept,
    reported_label, fiscal_year, fiscal_quarter, period_start, period_end,
    value_raw, currency, unit, is_restated, restated_from_line_item_id,
    source_page, extraction_method, confidence, raw_text, extracted_at,
    parser_version, reviewed_by_human, review_notes, period_type, fiscal_half
)
SELECT
    gen_random_uuid(),
    (SELECT company_id FROM core.companies WHERE ticker = '2320'),
    'e335e239-6dd9-4865-be72-072733fcf652',
    'income_statement',
    'net_income_attributable_to_parent',
    'صافي الربح (الخسارة) العائد لمساهمي المصدر',
    2025,
    NULL,
    NULL,
    '2025-12-31',
    453000,
    'SAR',
    'thousand',
    NULL,  -- is_restated: NULL = unknown/not checked (schema.sql's own Phase 0.5 rule), never FALSE-by-default
    NULL,
    NULL,  -- source_page: not applicable (web disclosure, not a paginated PDF)
    'manual_verified_official_disclosure',
    'HIGH',
    NULL,  -- raw_text: the extracted table itself was not captured verbatim into this SQL file
    now(),
    NULL,
    TRUE,
    'YoY change vs FY2024: +70.49% per the Tadawul announcement this row cites (anId=93565). '
    'Figure and attribution read/transcribed by the project owner directly from saudiexchange.sa '
    'via their own web_fetch tool — see the source_documents row above (document_sha256) for the '
    'exact provenance and its "extracted text, not raw HTML bytes" caveat.',
    'FY',
    NULL
WHERE NOT EXISTS (
    SELECT 1 FROM core.financial_line_items
    WHERE company_id = (SELECT company_id FROM core.companies WHERE ticker = '2320')
      AND document_id = 'e335e239-6dd9-4865-be72-072733fcf652'
      AND concept = 'net_income_attributable_to_parent'
      AND fiscal_year = 2025
);

-- ============================================================================
-- DEFERRED — not inserted in this batch, see header:
--   EIC (1303), FY2025, net_income_attributable_to_parent, 629,920 thousand
--     SAR (+56.80% YoY per Mubasher, itself citing Tadawul — not a
--     personally-verified primary-source URL yet).
--   ABO MOATI (4191), FY2025 (Apr-Mar fiscal year — uncertain calendar-year
--     alignment, flagged previously), net_income, 22,100 thousand SAR
--     (-18% YoY) — no primary-source URL personally verified yet.
-- ============================================================================
