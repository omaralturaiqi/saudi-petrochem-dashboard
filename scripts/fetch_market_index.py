#!/usr/bin/env python3
"""
scripts/fetch_market_index.py

Fetches daily closing values for the TASI market index (^TASI.SR on Yahoo
Finance) and writes ready-to-run INSERT statements for
core.market_indices (see schema_market_indices.sql — a proposed table, not
yet executed against Neon) to a local SQL file. This script NEVER writes to
Neon directly, and never touches core.companies or core.market_prices —
it is entirely self-contained, deliberately NOT importing
scripts/fetch_market_prices.py, so that script is never modified or coupled
to this one. The same structural pattern is copied by hand instead:
yearly-windowed requests, an inter-request delay, and a row-count sanity
check per year (see scripts/fetch_market_prices.py's own "GRANULARITY FIX"
note for why yearly windows are used instead of a single range=max call).

DATA SOURCE — read this before trusting the output:
  Yahoo Finance's public chart endpoint for ^TASI.SR is the same TIER 3
  aggregator source already used by scripts/fetch_market_prices.py for
  individual stock prices (see that script's own header for the full
  source-hierarchy rationale — the same applies here unchanged). Every row
  this script generates carries source='yahoo_finance' (literal, hardcoded).

WHAT THIS SCRIPT DOES:
  1. Requests Yahoo Finance's chart endpoint for ^TASI.SR ONE CALENDAR YEAR
     AT A TIME (period1=Jan 1 of that year, period2=Jan 1 of the next year,
     interval=1d), from YEAR_RANGE_START through the current year — never a
     single multi-year range=max call, for the same reason documented in
     scripts/fetch_market_prices.py: Yahoo silently downgrades interval=1d
     to a coarser granularity over a multi-year span, with no warning in
     the response itself.
  2. Extracts trade_date and close for each trading day in each year's
     response. Only close_value is needed for core.market_indices (unlike
     individual stocks, no open/high/low/volume/adjclose columns exist on
     this table).
  3. Skips (does not fabricate) any day where Yahoo's own response has a
     null close value for that index.
  4. Compares each year's row count against a dynamic expected minimum
     (proportional to that window's actual calendar-day span, capped at
     "today" for the current, in-progress year) — a suspiciously low
     nonzero count is logged as an explicit WARNING, not silently trusted;
     a genuine zero-row year is logged as informational.
  5. Writes batched `INSERT INTO core.market_indices (...) VALUES (...)
     ON CONFLICT (index_code, trade_date, source) DO NOTHING;` statements
     — matching schema_market_indices.sql's UNIQUE(index_code, trade_date,
     source) constraint exactly — to market_index_insert_statements.sql.

WHAT THIS SCRIPT DELIBERATELY DOES NOT DO:
  - Never calls any INSERT/UPDATE/DELETE/DDL against Neon — it has no Neon
    connection logic at all (unlike fetch_market_prices.py, this script
    does not even need a read-only core.companies lookup, since TASI is a
    fixed index_code, not something to resolve from a companies table).
  - Never touches core.companies, core.market_prices, or
    scripts/fetch_market_prices.py.
  - Never invents a row for a missing/failed day or a failed year — a
    failed year is logged and skipped, never approximated.

USAGE:
    python3 scripts/fetch_market_index.py
        # no environment variables required — this script never talks to
        # Neon at all, read or write.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timezone

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETASI.SR"
INDEX_CODE = "TASI"
SOURCE_LITERAL = "yahoo_finance"
OUTPUT_SQL_PATH = "market_index_insert_statements.sql"
INSERT_BATCH_SIZE = 250

YEAR_RANGE_START = 2010  # matches scripts/fetch_market_prices.py's convention;
                          # a year before the index has data simply yields 0
                          # rows (informational), not an error.
INTER_REQUEST_DELAY_SECONDS = 0.4
EXPECTED_TRADING_DAYS_FRACTION_OF_CALENDAR_DAYS = 0.5


def year_windows(start_year: int, end_year: int) -> list[tuple[int, int, int]]:
    """Pure, offline-testable: one (year, period1, period2) Unix-timestamp
    window per calendar year, start_year..end_year inclusive. period1 =
    Jan 1 00:00:00 UTC of that year; period2 = Jan 1 00:00:00 UTC of the
    NEXT year (exclusive upper bound — no overlap, no gap between
    consecutive windows). Identical logic to
    scripts/fetch_market_prices.py's year_windows(), duplicated here
    on purpose so that script is never imported or modified."""
    windows: list[tuple[int, int, int]] = []
    for year in range(start_year, end_year + 1):
        period1 = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
        period2 = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp())
        windows.append((year, period1, period2))
    return windows


def expected_min_rows(period1: int, period2: int) -> int:
    """Dynamic expected-minimum row count for a window, proportional to how
    many calendar days actually fall in it (capped at 'now' for a window
    extending into the future)."""
    now_ts = int(datetime.now(tz=timezone.utc).timestamp())
    effective_period2 = min(period2, now_ts)
    calendar_days = max(0, (effective_period2 - period1) // 86400)
    return int(calendar_days * EXPECTED_TRADING_DAYS_FRACTION_OF_CALENDAR_DAYS)


def fetch_yahoo_index_year(period1: int, period2: int) -> dict:
    """GET Yahoo Finance's chart endpoint for ^TASI.SR for one explicit
    calendar-year window, interval=1d."""
    import requests

    resp = requests.get(
        YAHOO_CHART_URL,
        params={"period1": period1, "period2": period2, "interval": "1d"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def extract_index_rows(chart_json: dict) -> list[dict]:
    """Parses one Yahoo Finance chart-endpoint response into a list of
    {"trade_date": ..., "close": ...} — only close is needed for
    core.market_indices. Any index with a null close is SKIPPED, never
    invented."""
    result = chart_json.get("chart", {}).get("result")
    if not result:
        error = chart_json.get("chart", {}).get("error")
        raise ValueError(f"Yahoo response has no 'result': error={error!r}")

    result0 = result[0]
    timestamps = result0.get("timestamp") or []
    quote = (result0.get("indicators", {}).get("quote") or [{}])[0]
    closes = quote.get("close", [])

    rows: list[dict] = []
    for i, ts in enumerate(timestamps):
        close_val = closes[i] if i < len(closes) else None
        if close_val is None:
            continue
        trade_date = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        rows.append({"trade_date": trade_date, "close": close_val})
    return rows


def _sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, date):
        return f"'{value.isoformat()}'"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(value)


def build_insert_sql(rows: list[dict], index_code: str = INDEX_CODE, source: str = SOURCE_LITERAL) -> list[str]:
    """Batched INSERT statements matching core.market_indices' column list
    and its UNIQUE(index_code, trade_date, source) constraint via
    ON CONFLICT ... DO NOTHING. retrieved_at is left to its DEFAULT now()."""
    statements: list[str] = []
    columns = "index_code, trade_date, close_value, source"
    for batch_start in range(0, len(rows), INSERT_BATCH_SIZE):
        batch = rows[batch_start:batch_start + INSERT_BATCH_SIZE]
        values_clauses = [
            "(" + ", ".join([
                _sql_literal(index_code),
                _sql_literal(r["trade_date"]),
                _sql_literal(r["close"]),
                _sql_literal(source),
            ]) + ")"
            for r in batch
        ]
        stmt = (
            f"INSERT INTO core.market_indices ({columns})\nVALUES\n    "
            + ",\n    ".join(values_clauses)
            + "\nON CONFLICT (index_code, trade_date, source) DO NOTHING;"
        )
        statements.append(stmt)
    return statements


def main() -> None:
    print("=" * 78)
    print("FETCH MARKET INDEX — TASI, Yahoo Finance (Tier 3 aggregator, stated interim source)")
    print("No Neon connection of any kind — this script never reads or writes Neon.")
    print("=" * 78)

    current_year = datetime.now(tz=timezone.utc).year
    windows = year_windows(YEAR_RANGE_START, current_year)
    print(f"Year windows: {YEAR_RANGE_START}-{current_year} ({len(windows)} requests)")
    print()

    all_rows: list[dict] = []

    for i, (year, period1, period2) in enumerate(windows):
        if i > 0:
            time.sleep(INTER_REQUEST_DELAY_SECONDS)
        try:
            chart_json = fetch_yahoo_index_year(period1, period2)
            year_rows = extract_index_rows(chart_json)
        except Exception as e:
            print(f"  {year}: FETCH FAILED: {type(e).__name__}: {e}")
            continue

        if not year_rows:
            print(f"  {year}: 0 rows (no data this year — not an error)")
            continue

        min_expected = expected_min_rows(period1, period2)
        if len(year_rows) < min_expected:
            print(
                f"  {year}: WARNING — {len(year_rows)} rows, expected at least "
                f"~{min_expected} for this window's calendar-day span. Not assumed "
                "valid or invalid; rows are still included below, not discarded, "
                "but this must be reviewed before treating them as reliable daily data."
            )
        else:
            print(f"  {year}: {len(year_rows)} rows (OK, >= expected minimum ~{min_expected})")

        all_rows.extend(year_rows)

    if not all_rows:
        print("\n0 total rows extracted across all years — no SQL file written, nothing fabricated.")
        return

    all_rows.sort(key=lambda r: r["trade_date"])
    print()
    print(f"TOTAL rows extracted: {len(all_rows)}")
    print(f"date range: {all_rows[0]['trade_date']} .. {all_rows[-1]['trade_date']}")
    print("first 3 rows:")
    for r in all_rows[:3]:
        print(f"  {r['trade_date']}  close={r['close']}")
    print("last 3 rows:")
    for r in all_rows[-3:]:
        print(f"  {r['trade_date']}  close={r['close']}")

    statements = build_insert_sql(all_rows)
    with open(OUTPUT_SQL_PATH, "w", encoding="utf-8") as f:
        f.write(
            "-- market_index_insert_statements.sql\n"
            "-- Generated by scripts/fetch_market_index.py — NOT executed against Neon by\n"
            "-- this script. Source: Yahoo Finance (Tier 3 aggregator, see script header).\n"
            "-- Targets core.market_indices — see schema_market_indices.sql (proposed table,\n"
            "-- not yet executed against Neon as of this script's writing).\n"
            "-- Review before running manually (e.g. via Neon SQL Editor).\n\n"
        )
        f.write(f"-- TASI — {len(all_rows)} rows\n\n")
        f.write("\n\n".join(statements))
        f.write("\n")

    print()
    print("=" * 78)
    print(f"SQL written to: {OUTPUT_SQL_PATH}")
    print("NEON WRITES ISSUED BY THIS SCRIPT: 0")
    print("=" * 78)


if __name__ == "__main__":
    main()
