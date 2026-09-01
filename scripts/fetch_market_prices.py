#!/usr/bin/env python3
"""
scripts/fetch_market_prices.py

Fetches daily OHLCV price history for the companies already present in
core.companies (read-only lookup — tickers are never hardcoded here) from
Yahoo Finance, and writes ready-to-run INSERT statements for
core.market_prices to a local SQL file. This script NEVER writes to Neon
directly — no connection in this environment could reach it for writes even
if it tried; the SQL file is meant to be reviewed and run manually (e.g. via
the Neon SQL Editor), exactly like every other database write in this
project's history so far.

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

GRANULARITY FIX (this revision): the first live run of this script
(commit a61d863, executed via Render Shell) requested a single
range=max&interval=1d call per ticker and got back only ~198 rows spanning
~16 years (2010-03-31 .. 2026-09-01) — approximately 12 rows/year, i.e.
MONTHLY granularity, not the ~250 rows/year daily granularity requested.
This is a known Yahoo Finance behavior: when range=max (or any period1/
period2 span of several years) is combined with interval=1d, Yahoo silently
downgrades to a coarser granularity instead of honoring interval=1d, with
no error or warning in the response itself. The fix: request one
YEAR at a time (explicit period1/period2 per calendar year, still
interval=1d each), which stays well inside the span Yahoo will actually
honor at daily granularity, and check the row count returned for each
year against what a real trading year should contain.

WHAT THIS SCRIPT DOES:
  1. Reads (ticker, company_id) for every row in core.companies via Neon's
     SQL-over-HTTP endpoint (same read pattern as app.py/us_xbrl_api.py) —
     read-only SELECT only, no write capability is exercised against Neon
     anywhere in this script.
  2. For each company, requests Yahoo Finance's chart endpoint ONE
     CALENDAR YEAR AT A TIME (period1=Jan 1 of that year, period2=Jan 1 of
     the next year, interval=1d), from YEAR_RANGE_START through the
     current year — never a single multi-year range=max call (see
     "GRANULARITY FIX" above). A short delay is added between requests to
     avoid Yahoo rate-limiting, since each company now needs one request
     per year instead of one request total.
  3. Extracts trade_date (from each Unix timestamp), open, high, low,
     close, adjclose, and volume for each trading day in each year's
     response.
  4. Skips (does not fabricate) any day where Yahoo's own response has a
     null close price for that index — this happens for non-trading days
     included in the timestamp array, or genuine data gaps in Yahoo's own
     dataset. A skipped day is not a row in the output; nothing is
     invented to fill it.
  5. After extracting each year's rows, compares the count actually
     received against a dynamic expected minimum (proportional to how many
     calendar days actually fall in that year's window, capped at "today"
     for the current year) — a year with ZERO rows is logged as
     informational (genuinely no trading that year, e.g. before listing,
     is an entirely normal and expected case), but a NONZERO count well
     below the expected minimum is logged as an explicit WARNING (the
     monthly-granularity failure signature this fix targets) rather than
     being silently accepted as valid. The rows are still included in the
     output either way — this script does not know FOR CERTAIN that a
     low count is wrong, only that it looks suspicious — but the warning
     ensures it is never silently trusted.
  6. Writes batched `INSERT INTO core.market_prices (...) VALUES (...)
     ON CONFLICT (company_id, trade_date, source) DO NOTHING;` statements
     — matching core.market_prices' actual UNIQUE(company_id, trade_date,
     source) constraint exactly — to market_prices_insert_statements.sql.

WHAT THIS SCRIPT DELIBERATELY DOES NOT DO:
  - Never calls any INSERT/UPDATE/DELETE/DDL against Neon. The only Neon
    interaction is one read-only SELECT to look up existing companies.
  - Never touches corporate_actions (a separate, later task).
  - Never populates traded_value (Yahoo's chart endpoint does not report
    turnover directly; left NULL rather than derived/estimated) or
    is_delayed (left NULL — unverified, matching this schema's existing
    "NULL = not yet checked, never guessed" convention, e.g.
    market_prices.adjusted_close_price's own documented rule in schema.sql).
  - Never invents a row for a missing/failed day or a failed company fetch
    — a failed company is logged and skipped, never approximated.

USAGE:
    python3 scripts/fetch_market_prices.py
        # requires NEON_CONNECTION_STRING in the environment for the
        # read-only core.companies lookup (same variable app.py uses)
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date, datetime, timezone

YAHOO_CHART_URL_TEMPLATE = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}.SR"
OUTPUT_SQL_PATH = "market_prices_insert_statements.sql"
SOURCE_LITERAL = "yahoo_finance"
INSERT_BATCH_SIZE = 250

# --- yearly-window fetch tuning ---------------------------------------------
YEAR_RANGE_START = 2010  # per explicit instruction; harmless for a company
                          # not yet listed that early — Yahoo just returns 0
                          # rows for those years, logged as informational,
                          # not an error.
INTER_REQUEST_DELAY_SECONDS = 0.4  # each company now issues one HTTP request
                                     # per year instead of one total — this
                                     # delay is to avoid Yahoo rate-limiting.
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
            "This is required for the read-only core.companies lookup — "
            "same variable app.py uses. No write capability is implied by "
            "having this set; this script never issues INSERT/UPDATE/"
            "DELETE/DDL against Neon."
        )
    host = conn.split("@")[1].split("/")[0]
    return f"https://{host}/sql"


def fetch_companies_from_db() -> list[dict]:
    """Read-only SELECT against core.companies. Returns a list of
    {"ticker": ..., "company_id": ...} for every row that has a ticker —
    tickers are never hardcoded in this script; whatever is actually in
    the table is what gets fetched."""
    import requests

    neon_sql_url = get_neon_sql_url()
    resp = requests.post(
        neon_sql_url,
        headers={
            "Neon-Connection-String": os.environ["NEON_CONNECTION_STRING"],
            "Content-Type": "application/json",
        },
        json={"query": "SELECT ticker, company_id FROM core.companies WHERE ticker IS NOT NULL ORDER BY ticker;"},
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"core.companies lookup failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()["rows"]


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
            + f"\nON CONFLICT (company_id, trade_date, source) DO NOTHING;"
        )
        statements.append(stmt)
    return statements


def main() -> None:
    print("=" * 78)
    print("FETCH MARKET PRICES — Yahoo Finance (Tier 3 aggregator, stated interim source)")
    print("Read-only against Neon (core.companies lookup only). No Neon writes ever issued.")
    print("=" * 78)

    try:
        companies = fetch_companies_from_db()
    except Exception as e:
        print(f"FAILED to read core.companies: {type(e).__name__}: {e}")
        sys.exit(1)

    print(f"Companies found in core.companies: {len(companies)}")
    for c in companies:
        print(f"  ticker={c['ticker']!r} company_id={c['company_id']}")
    print()

    current_year = datetime.now(tz=timezone.utc).year
    windows = year_windows(YEAR_RANGE_START, current_year)
    print(f"Year windows: {YEAR_RANGE_START}-{current_year} ({len(windows)} requests per company)")
    print()

    all_statements: list[str] = []
    failed: list[tuple[str, str]] = []

    for c in companies:
        ticker = c["ticker"]
        company_id = c["company_id"]
        print(f"--- {ticker} ---")
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

        if not company_rows:
            print("  0 total rows extracted across all years — skipping, not fabricating any row")
            failed.append((ticker, "0 rows across all year windows"))
            print()
            continue

        company_rows.sort(key=lambda r: r["trade_date"])
        print(f"  TOTAL rows extracted: {len(company_rows)}")
        print(f"  date range: {company_rows[0]['trade_date']} .. {company_rows[-1]['trade_date']}")
        print("  first 3 rows:")
        for r in company_rows[:3]:
            print(f"    {r['trade_date']}  close={r['close']}")
        print("  last 3 rows:")
        for r in company_rows[-3:]:
            print(f"    {r['trade_date']}  close={r['close']}")

        statements = build_insert_sql(company_rows, company_id)
        all_statements.extend([f"-- {ticker}.SR — {len(company_rows)} rows"] + statements)
        print()

    with open(OUTPUT_SQL_PATH, "w", encoding="utf-8") as f:
        f.write(
            "-- market_prices_insert_statements.sql\n"
            "-- Generated by scripts/fetch_market_prices.py — NOT executed against Neon by\n"
            "-- this script. Source: Yahoo Finance (Tier 3 aggregator, see script header).\n"
            "-- Review before running manually (e.g. via Neon SQL Editor).\n\n"
        )
        f.write("\n\n".join(all_statements))
        f.write("\n")

    print("=" * 78)
    print(f"SQL written to: {OUTPUT_SQL_PATH}")
    print(f"Companies succeeded: {len(companies) - len(failed)}/{len(companies)}")
    if failed:
        print(f"Companies failed: {len(failed)}")
        for ticker, reason in failed:
            print(f"  {ticker}: {reason}")
    print("NEON WRITES ISSUED BY THIS SCRIPT: 0")
    print("=" * 78)


if __name__ == "__main__":
    main()
