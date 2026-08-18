-- ============================================================================
-- Migration: create-readonly-role.sql
-- Purpose: create a dedicated, minimum-privilege Postgres role for the web
--          application, replacing its current use of the neondb_owner
--          credential (audit finding, see AUDIT-REPORT / README §Security).
--
-- HOW TO RUN THIS (manual step — not executed automatically by any code):
--   1. Open the Neon Console → your project → SQL Editor
--      (or run via psql/any Postgres client connected with the owner role)
--   2. Paste and run this entire script once.
--   3. Neon Console → Roles → note the new role "app_readonly" now exists.
--   4. Neon Console → Connect / Connection Details → switch the role
--      selector to "app_readonly" → copy the connection string it generates.
--   5. In Render → saudi-petrochem-dashboard → Environment → update
--      NEON_CONNECTION_STRING to the new app_readonly connection string.
--      Render will redeploy automatically.
--   6. Verify the app still loads correctly, then optionally rotate/retire
--      any connection string that still uses neondb_owner if it was ever
--      pasted anywhere outside Neon's own dashboard.
--
-- This script is purely additive: it does not touch existing data, tables,
-- or the neondb_owner role. It only creates a new role and grants it the
-- minimum privileges the web app actually needs (CONNECT + USAGE + SELECT
-- on the tables app.py currently queries).
-- ============================================================================

-- Neon manages role passwords through its own console/API rather than a
-- plain CREATE ROLE ... PASSWORD statement in most workflows; if your Neon
-- project requires an explicit password here, replace 'CHANGE_ME_IN_NEON'
-- with a strong generated password, or — preferably — create the role
-- through the Neon Console's "Roles" tab instead of this raw SQL, which
-- will handle credential generation and rotation for you and avoids ever
-- having a plaintext password appear in a file at all.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_readonly') THEN
        CREATE ROLE app_readonly WITH LOGIN PASSWORD 'CHANGE_ME_IN_NEON';
    END IF;
END
$$;

-- Minimum connection privilege.
GRANT CONNECT ON DATABASE neondb TO app_readonly;

-- Schema-level access (required before table-level SELECT grants apply).
GRANT USAGE ON SCHEMA core TO app_readonly;

-- Read-only access to exactly the tables the current web app queries.
-- Deliberately NOT granting on financial_line_items/source_documents/
-- companies with anything beyond SELECT, and NOT granting on any other
-- table in core (concept_dictionary, market_prices, corporate_actions,
-- derived_metrics, validation_log, data_gaps, historical_universe) until
-- the application actually needs to read them — grant additively later,
-- following the same principle: minimum privilege for what's actually used.
GRANT SELECT ON core.companies TO app_readonly;
GRANT SELECT ON core.source_documents TO app_readonly;
GRANT SELECT ON core.financial_line_items TO app_readonly;

-- Explicitly confirm no write privileges exist (this is the default for a
-- freshly created role with only the grants above, but stated explicitly
-- here so the intent is unambiguous to anyone reading this file later).
-- No INSERT, UPDATE, DELETE, TRUNCATE, or DDL privileges are granted.
-- No CREATEDB, CREATEROLE, or REPLICATION attributes are set (defaults to
-- false for a role created without those clauses).

-- Verification query (safe to run, read-only) — run this after the above
-- to confirm the role was created with exactly the intended privileges:
--
--   SELECT grantee, table_name, privilege_type
--   FROM information_schema.role_table_grants
--   WHERE grantee = 'app_readonly'
--   ORDER BY table_name;
--
-- Expected output: exactly 3 rows, all privilege_type = 'SELECT', for
-- companies, source_documents, and financial_line_items.
