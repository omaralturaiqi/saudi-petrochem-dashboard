#!/usr/bin/env python3
"""
scripts/fetch_market_prices.py

Fetches daily OHLCV price history for the companies already present in
core.companies (read-only lookup — tickers are never hardcoded here) from
Yahoo Finance, and writes ready-to-run INSERT statements for
core.market_prices — one SQL file PER COMPANY — to disk. This script NEVER
writes to Neon directly — no connection in this environment could reach it
for writes even if it tried; each SQL file is meant to be reviewed and run
manually (e.g. via the Neon SQL Editor / Render Shell), exactly like every
other database write in this project's history so far.

DATA SOURCE — read this before trusting the output:
  Yahoo Finance's public chart endpoint
  (https://query1.finance.yahoo.com/v8/finance/chart/{TICKER}.SR) is a
  TIER 3 aggregator source per this project's own source hierarchy
  (README.md / schema.sql's source_tier convention: 1=official filing/
  exchange, 2=official re-publication, 3=secondary/normalized aggregator,
  4=derived). This is a STATED INTERIM CHOICE, not a claim that Yahoo
  Finance is authoritative — Tadawul/official exchange data (Tier 1) was
  not available for automated bulk fetch in the session that wrote this
  script. Every row this script generates carries source='yahoo_finance'
  (literal, hardcoded) in the `source` column specifically so this can
  never be silently confused with a higher-tier source later.

GRANULARITY FIX (carried over unchanged from the prior revision): the
first live run of this script (commit a61d863, executed via Render Shell)
requested a single range=max&interval=1d call per ticker and got back only
~198 rows spanning ~16 years — approximately 12 rows/year, i.e. MONTHLY
granularity, not the ~250 rows/year daily granularity requested. Yahoo
silently downgrades interval=1d to a coarser granularity over a multi-year
span, with no error/warning in the response itself. The fix: request one
YEAR at a time (explicit period1/period2 per calendar year, interval=1d
each), which stays inside the span Yahoo will actually honor at daily
granularity.

SCALE-UP (this revision): the prior revision hardcoded 3 companies
(SABIC/YANSAB/Advanced) implicitly by whatever was in core.companies at
the time; core.companies now holds 253 companies (still read live, never
hardcoded — no change needed there). At 17 year-windows x up to 253
companies (~4,300 requests total), a single uninterrupted run is no longer
a safe assumption, so this revision adds:
  1. A CHECKPOINT/RESUME file (fetch_progress.txt, see PROGRESS_FILE_PATH):
     one completed ticker per line, appended immediately after that
     company's SQL file is fully written. On startup, this file is read
     first and every ticker in it is skipped — a run can be interrupted
     (Ctrl-C, container restart, Render Shell session ending) and resumed
     later without re-fetching companies already done. See
     "WHAT COUNTS AS 'COMPLETED'" below for the exact rule.
  2. A live, verified (not assumed) check against core.market_prices for
     which companies already have ANY price rows — those are skipped and
     reported, independent of and in addition to the checkpoint file (a
     second, live-verified line of defense in case fetch_progress.txt was
     lost, e.g. a fresh container). This generalizes the original
     instruction to skip "the 3 original companies if they already have
     data" to ALL companies, since the same reasoning (don't blindly
     re-fetch, don't blindly assume — check) applies equally to any
     company a prior interrupted run may have already completed.
  3. Per-company try/except around the ENTIRE per-company block (not just
     per-year, which already existed): an unexpected exception for one
     company is caught, logged as FAILED, and the run continues to the
     next company — never aborts the whole run over one company.
  4. One SQL file PER COMPANY (market_prices_insert_<ticker>.sql) instead
     of one combined file for all companies — written immediately after
     that company finishes, not held in memory until the end. This bounds
     the damage of a mid-run interruption to at most the one company in
     progress, and makes partial, incremental review/execution possible
     without waiting for the full 253-company run to finish.

WHAT COUNTS AS "COMPLETED" (for the checkpoint file):
  A company is appended to fetch_progress.txt once its per-year loop has
  been attempted for ALL windows (2010..current year) without an
  unhandled exception escaping the per-company try/except, AND at least
  one row was extracted (so its SQL file is non-empty and was written).
  Individual per-year failures/warnings within that loop do NOT prevent
  checkpointing — that behavior (log and continue) is unchanged from the
  prior revision. A company that yields ZERO rows across every window is
  NOT checkpointed (so a future run retries it — it may indicate a bad
  ticker mapping worth re-checking, not a company legitimately without
  data) and is reported as FAILED, per this task's explicit "don't
  silently skip a company without reporting it" requirement.

WHAT THIS SCRIPT DOES:
  1. Reads (ticker, company_id) for every row in core.companies via Neon's
     SQL-over-HTTP endpoint (same read pattern as app.py/us_xbrl_api.py) —
     read-only SELECT only, no write capability is exercised against Neon
     anywhere in this script.
  2. Reads (read-only) which tickers already have at least one row in
     core.market_prices, and skips those — logged, not silent.
  3. Reads fetch_progress.txt (if present) and skips any ticker already
     listed there — logged, not silent.
  4. For each remaining company, requests Yahoo Finance's chart endpoint
     ONE CALENDAR YEAR AT A TIME (period1=Jan 1 of that year, period2=Jan
     1 of the next year, interval=1d), from YEAR_RANGE_START through the
     current year. A short delay is added between requests to avoid
     Yahoo rate-limiting.
  5. Extracts trade_date, open, high, low, close, adjclose, and volume for
     each trading day in each year's response.
  6. Skips (does not fabricate) any day where Yahoo's own response has a
     null close price — this happens for non-trading days included in the
     timestamp array, or genuine data gaps in Yahoo's own dataset. A
     skipped day is not a row in the output; nothing is invented.
  7. Compares each year's extracted row count against a dynamic expected
     minimum (proportional to how many calendar days actually fall in
     that year's window, capped at "today" for the current year) — a
     ZERO-row year is informational (e.g. before listing), a NONZERO
     count well below the expected minimum is an explicit WARNING (the
     monthly-granularity failure signature), never silently trusted. Rows
     are still included either way.
  8. Writes batched `INSERT INTO core.market_prices (...) VALUES (...)
     ON CONFLICT (company_id, trade_date, source) DO NOTHING;` statements
     — matching core.market_prices' actual UNIQUE(company_id, trade_date,
     source) constraint exactly — to market_prices_insert_<ticker>.sql,
     one file per company, and appends the ticker to fetch_progress.txt
     (see "WHAT COUNTS AS 'COMPLETED'" above).

WHAT THIS SCRIPT DELIBERATELY DOES NOT DO:
  - Never calls any INSERT/UPDATE/DELETE/DDL against Neon. The only Neon
    interaction is two read-only SELECTs (core.companies,
    core.market_prices existence check).
  - Never touches corporate_actions (a separate, later task).
  - Never populates traded_value (Yahoo's chart endpoint does not report
    turnover directly; left NULL rather than derived/estimated) or
    is_delayed (left NULL — unverified, matching this schema's existing
    "NULL = not yet checked, never guessed" convention).
  - Never invents a row for a missing/failed day, a failed year, or a
    failed company — a failed company is logged clearly as FAILED and the
    run continues to the next one, never silently dropped from the report.
  - Never silently skips a company without saying so and why (checkpoint
    resume / already-has-data / zero-rows-across-all-years are each
    reported under a distinct, explicit label — never merged into a
    single unexplained "skipped").

USAGE:
    python3 scripts/fetch_market_prices.py
        # requires NEON_CONNECTION_STRING in the environment for the two
        # read-only lookups (same variable app.py uses). Safe to interrupt
        # (Ctrl-C) and re-run — see CHECKPOINT/RESUME above.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date, datetime, timezone

YAHOO_CHART_URL_TEMPLATE = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}.SR"
OUTPUT_SQL_PATH_TEMPLATE = "market_prices_insert_{ticker}.sql"
PROGRESS_FILE_PATH = "fetch_progress.txt"
SOURCE_LITERAL = "yahoo_finance"
INSERT_BATCH_SIZE = 250

# --- yearly-window fetch tuning ---------------------------------------------
YEAR_RANGE_START = 2010  # per explicit instruction; harmless for a company
                          # not yet listed that early — Yahoo just returns 0
                          # rows for those years, logged as informational,
                          # not an error.
INTER_REQUEST_DELAY_SECONDS = 0.4  # each company issues one HTTP request per
                                     # year — this delay is to avoid Yahoo
                                     # rate-limiting across ~4,300 requests.
# A full trading year is ~250-260 sessions (holidays/weekends excluded).
# Used as a per-calendar-day rate to build a dynamic expected-minimum for
# partial windows (e.g. the current, still-in-progress year) without
# hardcoding a single full-year number that would false-positive on those.
EXPECTED_TRADING_DAYS_FRACTION_OF_CALENDAR_DAYS = 0.5


def get_neon_sql_url() -> str:
    conn = os.environ.get("NEON_CONNECTION_STRING")
    if not conn:
        raise RuntimeError(
            "NEON_CONNECTION_STRING environment variable is not set. "
            "This is required for the two read-only lookups this script "
            "performs (core.companies, core.market_prices existence check) "
            "— same variable app.py uses. No write capability is implied "
            "by having this set; this script never issues INSERT/UPDATE/"
            "DELETE/DDL against Neon."
        )
    host = conn.split("@")[1].split("/")[0]
    return f"https://{host}/sql"


def run_query(sql: str) -> list[dict]:
    """Read-only helper — every call site in this script issues SELECT
    only, never INSERT/UPDATE/DELETE/DDL."""
    import requests

    neon_sql_url = get_neon_sql_url()
    resp = requests.post(
        neon_sql_url,
        headers={
            "Neon-Connection-String": os.environ["NEON_CONNECTION_STRING"],
            "Content-Type": "application/json",
        },
        json={"query": sql},
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"query failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()["rows"]


def fetch_companies_from_db() -> list[dict]:
    """Read-only SELECT against core.companies. Returns a list of
    {"ticker": ..., "company_id": ...} for every row that has a ticker —
    tickers are never hardcoded in this script; whatever is actually in
    the table is what gets fetched. Now returns up to 253 rows (was 3 at
    the time the prior revision of this script was written) — no change
    needed here, this query was already generic."""
    return run_query("SELECT ticker, company_id FROM core.companies WHERE ticker IS NOT NULL ORDER BY ticker;")


def fetch_tickers_with_existing_price_data() -> set[str]:
    """Read-only SELECT: which tickers already have at least one row in
    core.market_prices, verified live against Neon — never assumed. Used
    to skip a company a prior run (this session's earlier 3-company run,
    or an earlier interrupted attempt at this 253-company run) already
    populated, so this script does not blindly re-fetch ~4,300 requests'
    worth of data that's already there."""
    rows = run_query(
        "SELECT DISTINCT c.ticker FROM core.market_prices mp "
        "JOIN core.companies c ON c.company_id = mp.company_id "
        "WHERE c.ticker IS NOT NULL;"
    )
    return {r["ticker"] for r in rows}


def load_completed_tickers(path: str) -> set[str]:
    """Pure, offline-testable (given a path). Reads the checkpoint file —
    one ticker per line — and returns the set of tickers already marked
    complete. Missing file means no prior progress, not an error."""
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def append_completed_ticker(path: str, ticker: str) -> None:
    """Appends one ticker to the checkpoint file immediately after that
    company's SQL file has been fully written — so an interruption right
    after this call still leaves both the SQL file and the checkpoint
    entry consistent with each other."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(ticker + "\n")


def year_windows(start_year: int, end_year: int) -> list[tuple[int, int, int]]:
    """Pure, offline-testable: builds one (year, period1, period2) Unix-
    timestamp window per calendar year from start_year through end_year
    inclusive. period1 = Jan 1 00:00:00 UTC of that year; period2 = Jan 1
    00:00:00 UTC of the NEXT year (exclusive upper bound, covers all of
    `year` with no overlap and no gap between consecutive windows)."""
    windows: list[tuple[int, int, int]] = []
    for year in range(start_year, end_year + 1):
        period1 = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
        period2 = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp())
        windows.append((year, period1, period2))
    return windows


def expected_min_rows(period1: int, period2: int) -> int:
    """Dynamic expected-minimum row count for a window, proportional to how
    many calendar days actually fall in it (capped at 'now' for a window
    that extends into the future, e.g. the current, in-progress year) —
    avoids false-positive warnings on a legitimately partial year."""
    now_ts = int(datetime.now(tz=timezone.utc).timestamp())
    effective_period2 = min(period2, now_ts)
    calendar_days = max(0, (effective_period2 - period1) // 86400)
    return int(calendar_days * EXPECTED_TRADING_DAYS_FRACTION_OF_CALENDAR_DAYS)


def fetch_yahoo_chart_year(ticker: str, period1: int, period2: int) -> dict:
    """GET Yahoo Finance's chart endpoint for {ticker}.SR for ONE explicit
    calendar-year window (period1/period2 as Unix timestamps), interval=1d.
    See module docstring's "GRANULARITY FIX" — a single multi-year
    range=max call was found (via a real Render Shell run) to make Yahoo
    silently downgrade to monthly granularity; a one-year-at-a-time window
    stays inside the span Yahoo actually honors interval=1d for."""
    import requests

    url = YAHOO_CHART_URL_TEMPLATE.format(ticker=ticker)
    resp = requests.get(
        url,
        params={"period1": period1, "period2": period2, "interval": "1d"},
        headers={"User-Agent": "Mozilla/5.0"},  # Yahoo's endpoint rejects requests with no User-Agent
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def extract_price_rows(chart_json: dict) -> list[dict]:
    """Parses one Yahoo Finance chart-endpoint response into a list of
    per-trading-day dicts: trade_date, open, high, low, close, adjclose,
    volume. Any index where Yahoo's own 'close' value is null is SKIPPED
    entirely — never filled with an invented/interpolated value."""
    result = chart_json.get("chart", {}).get("result")
    if not result:
        error = chart_json.get("chart", {}).get("error")
        raise ValueError(f"Yahoo response has no 'result': error={error!r}")

    result0 = result[0]
    timestamps = result0.get("timestamp") or []
    quote = (result0.get("indicators", {}).get("quote") or [{}])[0]
    adjclose_block = (result0.get("indicators", {}).get("adjclose") or [{}])[0]

    opens = quote.get("open", [])
    highs = quote.get("high", [])
    lows = quote.get("low", [])
    closes = quote.get("close", [])
    volumes = quote.get("volume", [])
    adjcloses = adjclose_block.get("adjclose", [None] * len(timestamps))

    rows: list[dict] = []
    for i, ts in enumerate(timestamps):
        close_val = closes[i] if i < len(closes) else None
        if close_val is None:
            # No real closing price for this index — skip, do not invent.
            continue
        trade_date = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        rows.append({
            "trade_date": trade_date,
            "open": opens[i] if i < len(opens) else None,
            "high": highs[i] if i < len(highs) else None,
            "low": lows[i] if i < len(lows) else None,
            "close": close_val,
            "adjclose": adjcloses[i] if i < len(adjcloses) else None,
            "volume": volumes[i] if i < len(volumes) else None,
        })
    return rows


def _sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, date):
        return f"'{value.isoformat()}'"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(value)


def build_insert_sql(rows: list[dict], company_id: str, source: str = SOURCE_LITERAL) -> list[str]:
    """Builds batched INSERT statements (INSERT_BATCH_SIZE rows per
    statement) matching core.market_prices' real column list and its
    actual UNIQUE(company_id, trade_date, source) constraint via
    ON CONFLICT ... DO NOTHING. traded_value and is_delayed are always
    NULL — this script has no source for either and does not derive/
    guess them. retrieved_at is left to its DEFAULT now()."""
    statements: list[str] = []
    columns = (
        "company_id, trade_date, open_price, high_price, low_price, "
        "close_price, adjusted_close_price, volume, traded_value, "
        "is_delayed, source, source_id"
    )
    for batch_start in range(0, len(rows), INSERT_BATCH_SIZE):
        batch = rows[batch_start:batch_start + INSERT_BATCH_SIZE]
        values_clauses = []
        for r in batch:
            values_clauses.append(
                "(" + ", ".join([
                    _sql_literal(company_id),
                    _sql_literal(r["trade_date"]),
                    _sql_literal(r["open"]),
                    _sql_literal(r["high"]),
                    _sql_literal(r["low"]),
                    _sql_literal(r["close"]),
                    _sql_literal(r["adjclose"]),
                    _sql_literal(r["volume"]),
                    "NULL",  # traded_value — not reported by this source, not derived
                    "NULL",  # is_delayed — unverified, not guessed
                    _sql_literal(source),
                    "NULL",  # source_id — no source_documents row applies to a price fetch
                ]) + ")"
            )
        stmt = (
            f"INSERT INTO core.market_prices ({columns})\nVALUES\n    "
            + ",\n    ".join(values_clauses)
            + "\nON CONFLICT (company_id, trade_date, source) DO NOTHING;"
        )
        statements.append(stmt)
    return statements


def write_company_sql_file(ticker: str, company_rows: list[dict], company_id: str) -> str:
    """Writes one SQL file for this company only. Returns the path
    written. Called immediately after a company finishes — not batched
    with any other company — so a mid-run interruption never loses a
    completed company's already-written output."""
    path = OUTPUT_SQL_PATH_TEMPLATE.format(ticker=ticker)
    statements = build_insert_sql(company_rows, company_id)
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            f"-- {path}\n"
            f"-- Generated by scripts/fetch_market_prices.py — NOT executed against Neon by\n"
            f"-- this script. Source: Yahoo Finance (Tier 3 aggregator, see script header).\n"
            f"-- ticker={ticker} company_id={company_id} rows={len(company_rows)}\n"
            f"-- Review before running manually (e.g. via Neon SQL Editor / Render Shell).\n\n"
        )
        f.write("\n\n".join(statements))
        f.write("\n")
    return path


def process_company(ticker: str, company_id: str, windows: list[tuple[int, int, int]]) -> list[dict]:
    """Runs the full yearly-window fetch for one company. Returns the
    extracted rows (possibly empty). Raises nothing itself for a bad
    year — that is caught and logged per-window, same as before; a truly
    unexpected exception (e.g. a bug, not a single bad HTTP call) is left
    to propagate to the caller's own try/except (see main()), which is
    what actually marks this company FAILED rather than checkpointed."""
    company_rows: list[dict] = []
    for i, (year, period1, period2) in enumerate(windows):
        if i > 0:
            time.sleep(INTER_REQUEST_DELAY_SECONDS)
        try:
            chart_json = fetch_yahoo_chart_year(ticker, period1, period2)
            year_rows = extract_price_rows(chart_json)
        except Exception as e:
            # A single bad year does not abort the whole company — log
            # and continue to the next year, same "don't fabricate,
            # don't silently drop the rest" principle as before.
            print(f"  {year}: FETCH FAILED: {type(e).__name__}: {e}")
            continue

        if not year_rows:
            print(f"  {year}: 0 rows (no trading data this year — e.g. before listing; not an error)")
            continue

        min_expected = expected_min_rows(period1, period2)
        if len(year_rows) < min_expected:
            print(
                f"  {year}: WARNING — {len(year_rows)} rows, expected at least "
                f"~{min_expected} for this window's calendar-day span. This is the "
                "monthly-granularity failure signature this fix targets, OR a "
                "genuine partial-listing/trading-halt year — not assumed to be "
                "either; rows are still included below, not discarded, but this "
                "must be reviewed before treating them as reliable daily data."
            )
        else:
            print(f"  {year}: {len(year_rows)} rows (OK, >= expected minimum ~{min_expected})")

        company_rows.extend(year_rows)

    return company_rows


def main() -> None:
    print("=" * 78)
    print("FETCH MARKET PRICES — Yahoo Finance (Tier 3 aggregator, stated interim source)")
    print("Read-only against Neon (core.companies + core.market_prices lookups only).")
    print("No Neon writes ever issued. Safe to interrupt and re-run (see fetch_progress.txt).")
    print("=" * 78)

    try:
        companies = fetch_companies_from_db()
    except Exception as e:
        print(f"FAILED to read core.companies: {type(e).__name__}: {e}")
        sys.exit(1)

    try:
        already_has_data = fetch_tickers_with_existing_price_data()
    except Exception as e:
        print(f"FAILED to read core.market_prices for existing-data check: {type(e).__name__}: {e}")
        sys.exit(1)

    completed_tickers = load_completed_tickers(PROGRESS_FILE_PATH)

    print(f"Companies found in core.companies: {len(companies)}")
    print(f"Tickers already with data in core.market_prices (verified live, skipped): {sorted(already_has_data)}")
    print(f"Tickers already checkpointed in {PROGRESS_FILE_PATH} (resume, skipped): {sorted(completed_tickers)}")
    print()

    current_year = datetime.now(tz=timezone.utc).year
    windows = year_windows(YEAR_RANGE_START, current_year)
    print(f"Year windows: {YEAR_RANGE_START}-{current_year} ({len(windows)} requests per company)")
    print()

    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []
    skipped_existing_data: list[str] = []
    skipped_checkpoint: list[str] = []

    for c in companies:
        ticker = c["ticker"]
        company_id = c["company_id"]

        if ticker in already_has_data:
            print(f"--- {ticker}: SKIPPED — already has data in core.market_prices ---")
            skipped_existing_data.append(ticker)
            continue

        if ticker in completed_tickers:
            print(f"--- {ticker}: SKIPPED — already checkpointed in {PROGRESS_FILE_PATH} (resume) ---")
            skipped_checkpoint.append(ticker)
            continue

        print(f"--- {ticker} ---")
        try:
            company_rows = process_company(ticker, company_id, windows)
        except Exception as e:
            # Catches anything NOT already handled per-year inside
            # process_company (e.g. a genuinely unexpected bug) so one
            # company can never take down the rest of a 253-company run.
            print(f"  FAILED (unexpected error, company-level): {type(e).__name__}: {e}")
            failed.append((ticker, f"unexpected error: {type(e).__name__}: {e}"))
            print()
            continue

        if not company_rows:
            print("  0 total rows extracted across all years — skipping, not fabricating any row")
            print("  NOT checkpointed — a future run will retry this ticker")
            failed.append((ticker, "0 rows across all year windows"))
            print()
            continue

        company_rows.sort(key=lambda r: r["trade_date"])
        print(f"  TOTAL rows extracted: {len(company_rows)}")
        print(f"  date range: {company_rows[0]['trade_date']} .. {company_rows[-1]['trade_date']}")

        sql_path = write_company_sql_file(ticker, company_rows, company_id)
        append_completed_ticker(PROGRESS_FILE_PATH, ticker)
        succeeded.append(ticker)
        print(f"  SQL written to: {sql_path}")
        print(f"  Checkpointed in {PROGRESS_FILE_PATH}")
        print()

    print("=" * 78)
    print(f"Companies succeeded (SQL written + checkpointed): {len(succeeded)}/{len(companies)}")
    print(f"Companies skipped (already had data in core.market_prices): {len(skipped_existing_data)}")
    print(f"Companies skipped (already checkpointed — resume): {len(skipped_checkpoint)}")
    if failed:
        print(f"Companies FAILED (0 rows or unexpected error — NOT checkpointed, will retry next run): {len(failed)}")
        for ticker, reason in failed:
            print(f"  {ticker}: {reason}")
    print("NEON WRITES ISSUED BY THIS SCRIPT: 0")
    print("=" * 78)


if __name__ == "__main__":
    main()
