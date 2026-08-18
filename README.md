# Saudi Petrochemical Intelligence — Data Foundation

A research infrastructure project for Saudi-listed petrochemical companies, built on the principle:

```
Official source → Document → Evidence → Raw Fact → Derived Metric → Operational Driver
→ Cycle Analysis → Hypothesis → Scenario → Valuation → Investment Conclusion
```

**This README distinguishes CURRENT (what exists and works today) from PLANNED (future phases).
Nothing described as CURRENT is aspirational — every claim below was verified against the live
system as of this Phase 0 hardening pass.**

---

## CURRENT: What This Actually Is Right Now

A **read-only web viewer** over a small, manually-loaded, but genuinely source-verified financial
dataset for 3 companies (YANSAB, Advanced Petrochemical, SABIC — SABIC currently has a company
record but zero financial line items loaded). 13 financial facts total, every one individually
cross-checked against the official company annual report PDF by page number before being loaded.

It is **not yet** an investment analysis engine, has **no** operational/commodity data, **no**
valuation logic, and **no** scenario or hypothesis tooling. See "Planned" below for what's next.

## CURRENT: Architecture

```
Browser
  → Render (Flask app, gunicorn, free plan, Frankfurt region)
  → Neon Postgres, accessed over its HTTP SQL endpoint (not raw TCP)
  → PostgreSQL 18, schema "core"
```

**Why HTTP instead of a normal TCP connection to Postgres:** the development environment this was
originally built in could not reach Neon's standard port 5432 (outbound TCP was blocked; HTTPS on
443 was not). Neon's HTTP SQL endpoint (`https://<host>/sql`, documented at
https://neon.com/docs/serverless/serverless-driver) was used as a working alternative and is also a
reasonable permanent choice for a small, mostly-idle Render free-tier service, since it has no
persistent connection pool to re-establish after the service sleeps and wakes. `requests` is used
directly rather than the JS `@neondatabase/serverless` package since this is a Python app — the
underlying HTTP protocol is the same one that package uses internally.

**Important operational detail:** each HTTP call to this endpoint is a fully independent connection
— `SET search_path` or any session state from one call does not persist to the next. All queries in
this codebase use fully-qualified `core.table_name` references for this reason; this must be
preserved in any future query added to the codebase.

## CURRENT: Database Schema

See [`db/schema.sql`](db/schema.sql) — this is the verified, reproducible source of truth,
cross-checked column-by-column, constraint-by-constraint, and index-by-index against the live Neon
database as part of this Phase 0 hardening pass (see the verification note at the top of that file).

10 tables exist in the `core` schema:

| Table | Purpose | Rows (as of this pass) |
|---|---|---|
| `companies` | Master entity list | 3 |
| `source_documents` | Hashed, provenance-tracked source PDFs | 2 |
| `financial_line_items` | The flexible fact table — every extracted number, one row each | 13 |
| `concept_dictionary` | Controlled vocabulary for financial concepts | 11 |
| `historical_universe` | Survivorship-bias tracking (delisted/merged companies) | 0 |
| `market_prices` | Daily OHLCV | 0 |
| `corporate_actions` | Bonus shares, splits, rights issues | 0 |
| `derived_metrics` | Computed metrics (margins, growth, ratios) — strictly separate from reported facts | 0 |
| `validation_log` | Cross-source validation audit trail | 0 |
| `data_gaps` | First-class tracking of known missing data | 0 |

**Design principle already in place and must be preserved:** `financial_line_items` uses a flexible
`concept` string column rather than fixed columns like `revenue`/`cogs`/`gross_profit` — this is
what allows a bank's `net_interest_margin` or an insurer's `net_earned_premium` to be stored later
without a schema migration. Do not "normalize" this into fixed columns in a future phase.

Every fact in `financial_line_items` carries: which document it came from (`document_id`), which
page (`source_page`), how it was extracted (`extraction_method`), how confident the extraction is
(`confidence`: HIGH/MEDIUM/LOW), the original text it was parsed from (`raw_text`), and whether it's
known/unknown to be a restatement (`is_restated` — nullable, meaning "not yet checked," never
defaulted to false).

## CURRENT: Data Provenance — the 13 facts currently loaded

Every number currently in `financial_line_items` was manually cross-checked against the official
annual report PDF, page by page, before loading:

- **YANSAB** (ticker 2290): revenue, net_income, eps_basic, total_assets, total_equity for FY2023
  and FY2024 — 9 values, all exact matches against
  `yansab.com.sa`'s official FY2024 consolidated financial statements PDF.
- **Advanced Petrochemical** (ticker 2330): net_income and eps_basic for FY2023/FY2024 — 4 values,
  exact matches against `ir.advancedpetrochem.com`'s official FY2024 annual report PDF.

Both source PDFs are SHA256-hashed in `source_documents` for integrity verification.

**SABIC has a `companies` row but zero rows in `financial_line_items`** — this is a real, currently
unresolved gap, not an oversight in this README. See "Known Limitations" below.

## PLANNED (not yet built — do not assume these exist)

Per the project's phased roadmap, roughly in order:

- **P1 (foundation, next):** backfill SABIC's missing data; extend historical depth toward 2015 for
  all three currently-loaded companies; add the other 7 target companies (SABIC Agri-Nutrients,
  Saudi Kayan, Sipchem, Tasnee, SIIG, Nama Chemicals, Alujain).
- **P2 (data expansion):** new tables — `financial_periods`, `operational_metrics`,
  `commodity_prices`, `product_prices`, `feedstock_prices`, `spreads`, `projects`.
- **P3 (analytics):** populate `derived_metrics` — margins, YoY/QoQ growth, ratios (ROE, ROA, ROIC,
  Net Debt/EBITDA, FCF yield) — computed deterministically in code, never by an LLM, per this
  project's established computation/interpretation separation principle.
- **P4 (valuation/scenarios):** `assumptions`, `scenarios`, `hypotheses`, `analysis_evidence` tables
  and the logic to populate and query them — bear/base/bull cases, hypothesis testing
  (SUPPORTED/PARTIALLY SUPPORTED/NOT SUPPORTED/INSUFFICIENT DATA), cycle-position analysis.
- **P5 (UI/monitoring):** extending this viewer, or replacing it, to surface everything above. The
  UI is deliberately the last layer built, not the first — see "Architectural Principle" below.

None of P1–P5 exist in this codebase yet. If you see a table name from that list mentioned
somewhere and wonder why it's not queryable — it's planned, not built.

## Architectural Principle: Why the UI Is Last

This project prioritizes: **Data Foundation → Evidence → Normalization → Derived Metrics →
Analytics → Valuation → Investment Intelligence → UI**. The current Flask app is intentionally
minimal — three read-only queries and a template — because building visualization or analysis
features on top of 13 manually-loaded facts would produce a convincing-looking interface with
almost nothing real behind it. Expanding the data foundation (P1–P2) is higher priority than
improving this viewer.

## Security

**CURRENT status (as of this Phase 0 pass):**

- The Flask app queries Neon using the `NEON_CONNECTION_STRING` environment variable, set in
  Render's Environment tab (not committed to this repo — see `.env.example` for the placeholder
  format).
- **A dedicated read-only database role (`app_readonly`) has been prepared** —
  see [`db/migrations/001_create_readonly_role.sql`](db/migrations/001_create_readonly_role.sql) —
  **but has not yet been applied to Neon or wired into Render's environment variable.** This is a
  manual step the project owner must perform (documented in that file's header). Until that step is
  done, the deployed app is still using the higher-privilege `neondb_owner` credential. This is a
  known, currently-open gap, stated plainly rather than implied to be fixed.
- Public error responses no longer leak exception text, SQL, or connection details — this was
  fixed in this pass and verified (see `app.py`'s exception handling; a real exception containing a
  fake secret was deliberately triggered in local testing and confirmed absent from the HTTP
  response body, while still appearing in server-side logs).
- The app performs three static SELECT queries with **zero user-supplied input** reaching SQL
  anywhere — so SQL injection risk is effectively nil today. This is a property of the current
  code having no query parameters at all, not a parameterization pattern that's been proven safe
  for future dynamic queries. **The first person adding a parameterized query (e.g. "show me
  company X" from a URL) should use `%s`-style parameter binding, matching the pattern already used
  server-side in the read-only role's grant design** — never string-format user input into SQL.
- The Render service currently has no IP restriction (`0.0.0.0/0`) and no application-level
  authentication. For 13 already-public financial figures this is low-stakes; it will need
  revisiting before any commercially sensitive derived analysis (P3+) is exposed through the same
  public URL.
- **A Neon connection string was pasted in plaintext during this project's chat-based development
  history.** Whether or not code-level fixes are complete, that credential should be rotated via
  the Neon Console independently of anything in this repository.

## Local Development

```bash
cp .env.example .env
# edit .env and fill in NEON_CONNECTION_STRING (use the app_readonly role's
# connection string once db/migrations/001_create_readonly_role.sql has been
# applied — see that file for how)

pip install -r requirements.txt
python app.py
# visit http://localhost:5000
```

## Deployment (current setup)

Render web service, connected to this GitHub repository, auto-deploys on push to `main`:
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app`
- Environment variable `NEON_CONNECTION_STRING` set in Render's dashboard (not in this repo)

## Reproducing the Database From Scratch

1. Create a Neon project (Postgres 18 or later).
2. Run [`db/schema.sql`](db/schema.sql) against it — statement by statement if using Neon's HTTP
   SQL endpoint, since that endpoint does not support multi-statement scripts (each `;`-terminated
   statement needs its own request; see the ingestion/parser development history in project chat
   logs for the exact pattern used, not yet extracted into a standalone script in this repo — see
   "Known Limitations").
3. Run [`db/migrations/001_create_readonly_role.sql`](db/migrations/001_create_readonly_role.sql).
4. Load data via the ingestion pipeline (see below) — currently a manual, partially-scripted
   process, not a single command.

## Ingestion Pipeline

**CURRENT:** [`ingestion/parser.py`](ingestion/parser.py) exists and is real, tested code — it
extracts financial figures from official PDF annual reports using `pdfplumber` and keyword-based
line matching. Read the extensive header comment in that file before trusting its output; it
documents specific, confirmed failure modes (label-phrasing variance across companies, a
previously-fixed false-positive bug, note-reference-number contamination, and a fragile
`revenue`-concept matching rule) rather than presenting the parser as more reliable than it's been
shown to be.

**What `parser.py` does today:** reads PDFs from `data/raw/<COMPANY>/<YEAR>/annual_report.pdf`,
writes extracted candidate rows to a local CSV (`data/processed/normalized_financials_v2.csv`).

**KNOWN GAP, stated honestly rather than papered over:** the step that takes parser output and
loads it into `core.financial_line_items` in Neon was performed as an ad-hoc script during
development and was **not preserved as a committed, reusable file**. The 13 rows currently in
production were loaded this way, but running `parser.py` today does not, by itself, update the
live database — a proper `ingestion/load_to_neon.py` (or similar) reading the parser's CSV output
and performing validated, idempotent inserts is a real, near-term piece of missing work, not
something this README is pretending already exists.

**Not yet attempted:** structural table extraction (camelot/tabula) as a more robust alternative to
line-based regex/keyword matching — investigated during development, blocked by a missing
`ghostscript` system dependency in that environment, not by a technical dead-end. This remains open
for a future phase.

## Known Limitations (Phase 0 status)

- SABIC has zero financial facts loaded despite having a company record.
- Only 2 of the eventual ~10 target companies have any financial data at all.
- Only FY2023/FY2024 loaded — the project's 2015→present historical-depth goal is far from met.
- The read-only database role is prepared but not yet applied (manual step required — see
  Security section above).
- No automated tests exist yet.
- The Neon-loading half of the ingestion pipeline is not yet a committed, reusable script (see
  "Ingestion Pipeline" above).
- No CI/dependency vulnerability scanning is configured.

## What This Project Deliberately Has NOT Built Yet (and why)

Per this project's phased approach, the following are intentionally absent, not overlooked:
authentication, a valuation or scenario engine, commodity/feedstock pricing integration, and any
UI beyond the current minimal viewer. Building these before the data foundation (P1–P2) is solid
would mean building convincing-looking features with no real data behind them — see "Architectural
Principle" above.
