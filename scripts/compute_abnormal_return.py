#!/usr/bin/env python3
"""
scripts/compute_abnormal_return.py

READ-ONLY. Computes, for each company already in core.companies and each
fiscal year in YEARS_TO_COMPUTE, the year's stock return, the TASI index's
return over the same calendar dates, and the abnormal return (stock return
minus index return) — per the methodology document's Section 20 definition:
Abnormal Return = Stock Return - Index Return.

This exists because a manual pass this session on SABIC's FY2022-FY2024
financials found a "return to profitability" pattern (net income
attributable to parent: +16.5M in 2022, -2.77M loss in 2023, +1.5M recovery
in 2024) that did NOT correspond to the stock price direction (the stock
fell roughly every year regardless: -23%, -7%, -20% per that manual read).
The open question this script exists to help answer: was that a sector-wide/
market-wide decline (TASI also falling), or something specific to SABIC?
Raw stock return alone cannot answer that — abnormal return (return net of
the market's own movement) is the right comparison.

REQUIRES LIVE DATA THIS SESSION DOES NOT HAVE ACCESS TO:
  - core.market_prices populated for the 3 companies (confirmed populated
    this session via a real Render Shell + Neon SQL Editor run — 12,294
    rows, per the task context this script was written from, though this
    session did not independently re-verify that count).
  - core.market_indices populated with TASI data — schema_market_indices.sql
    is a PROPOSED table as of this script's writing; it has not been
    created or populated yet. This script will fail cleanly (a clear error
    naming the missing table) if run before that table exists and is
    populated — it does not degrade to a guess.
This session has no NEON_CONNECTION_STRING and no network access to Neon at
all (established repeatedly this session), so this script could NOT be run
end-to-end here. Only its pure percentage-calculation logic
(compute_return() / compute_abnormal_return()) was verified offline against
synthetic numbers — see the task report for that verification, not
committed as part of this file.

WHAT THIS SCRIPT DOES:
  1. Read-only SELECT against core.companies (ticker, company_id) — no
     hardcoded tickers.
  2. For each company and each year in YEARS_TO_COMPUTE, read-only SELECT
     against core.market_prices for that company/year's first and last
     available trade_date and close_price within that calendar year.
  3. Read-only SELECT against core.market_indices for TASI's first and last
     available close_value within the same calendar year.
  4. stock_return = (year_end_close - year_start_close) / year_start_close
     index_return = (year_end_index - year_start_index) / year_start_index
     abnormal_return = stock_return - index_return
  5. Prints one plain-text table (year, ticker, stock_return%, index_return%,
     abnormal_return%), sorted by year.

WHAT THIS SCRIPT DELIBERATELY DOES NOT DO:
  - Never writes anything — no INSERT/UPDATE/DELETE/DDL against Neon
    anywhere in this file.
  - Never touches corporate_actions — a company/year missing a stock split
    or bonus-share adjustment in that period would show a distorted
    stock_return; this script does not attempt to detect or correct for
    that (a separate, later task per the project's own phased scope).
  - Never guesses a missing year-start/year-end price — if a company has no
    market_prices rows at all for a given year, that (year, ticker) row is
    skipped and printed as "NO DATA", never estimated or interpolated.

USAGE:
    python3 scripts/compute_abnormal_return.py
        # requires NEON_CONNECTION_STRING in the environment (read-only)
"""
from __future__ import annotations

import os
import sys

YEARS_TO_COMPUTE = [2022, 2023, 2024, 2025]
INDEX_CODE = "TASI"


def get_neon_sql_url() -> str:
    conn = os.environ.get("NEON_CONNECTION_STRING")
    if not conn:
        raise RuntimeError(
            "NEON_CONNECTION_STRING environment variable is not set. This "
            "script is read-only but still requires it, same as app.py."
        )
    host = conn.split("@")[1].split("/")[0]
    return f"https://{host}/sql"


def run_query(sql: str) -> list[dict]:
    """Read-only helper — this script issues SELECT only, never INSERT/
    UPDATE/DELETE/DDL."""
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


def fetch_companies() -> list[dict]:
    return run_query("SELECT ticker, company_id FROM core.companies WHERE ticker IS NOT NULL ORDER BY ticker;")


def fetch_company_year_bounds(company_id: str, year: int) -> dict | None:
    """Returns {"start_date":..., "start_close":..., "end_date":...,
    "end_close":...} for the first/last trade_date+close_price this
    company has in core.market_prices within `year`, or None if it has no
    rows for that year at all — never a guessed value."""
    rows = run_query(f"""
        WITH yearly AS (
            SELECT trade_date, close_price
            FROM core.market_prices
            WHERE company_id = '{company_id}'
              AND EXTRACT(YEAR FROM trade_date) = {year}
        )
        SELECT
            (SELECT trade_date FROM yearly ORDER BY trade_date ASC LIMIT 1) AS start_date,
            (SELECT close_price FROM yearly ORDER BY trade_date ASC LIMIT 1) AS start_close,
            (SELECT trade_date FROM yearly ORDER BY trade_date DESC LIMIT 1) AS end_date,
            (SELECT close_price FROM yearly ORDER BY trade_date DESC LIMIT 1) AS end_close;
    """)
    if not rows or rows[0]["start_close"] is None:
        return None
    return rows[0]


def fetch_index_year_bounds(index_code: str, year: int) -> dict | None:
    rows = run_query(f"""
        WITH yearly AS (
            SELECT trade_date, close_value
            FROM core.market_indices
            WHERE index_code = '{index_code}'
              AND EXTRACT(YEAR FROM trade_date) = {year}
        )
        SELECT
            (SELECT trade_date FROM yearly ORDER BY trade_date ASC LIMIT 1) AS start_date,
            (SELECT close_value FROM yearly ORDER BY trade_date ASC LIMIT 1) AS start_close,
            (SELECT trade_date FROM yearly ORDER BY trade_date DESC LIMIT 1) AS end_date,
            (SELECT close_value FROM yearly ORDER BY trade_date DESC LIMIT 1) AS end_close;
    """)
    if not rows or rows[0]["start_close"] is None:
        return None
    return rows[0]


def compute_return(start_value: float, end_value: float) -> float:
    """Pure, offline-testable: fractional return, e.g. 0.0532 for +5.32%.
    Never called with start_value == 0 by this script's own flow (a zero
    starting price would already be a data-integrity issue elsewhere, not
    something to silently divide through)."""
    return (end_value - start_value) / start_value


def compute_abnormal_return(stock_return: float, index_return: float) -> float:
    """Pure, offline-testable: Abnormal Return = Stock Return - Index Return."""
    return stock_return - index_return


def main() -> None:
    print("=" * 96)
    print("ABNORMAL RETURN — Stock Return vs TASI Index Return, per methodology Section 20")
    print("READ-ONLY: no writes issued against Neon anywhere in this script.")
    print("=" * 96)

    try:
        companies = fetch_companies()
    except Exception as e:
        print(f"FAILED to read core.companies: {type(e).__name__}: {e}")
        sys.exit(1)

    results: list[tuple[int, str, float, float, float]] = []
    no_data: list[tuple[int, str, str]] = []

    for year in YEARS_TO_COMPUTE:
        try:
            index_bounds = fetch_index_year_bounds(INDEX_CODE, year)
        except Exception as e:
            print(f"{year}: FAILED to read core.market_indices for {INDEX_CODE}: {type(e).__name__}: {e}")
            index_bounds = None

        for c in companies:
            ticker = c["ticker"]
            if index_bounds is None:
                no_data.append((year, ticker, f"no {INDEX_CODE} index data for {year}"))
                continue
            try:
                stock_bounds = fetch_company_year_bounds(c["company_id"], year)
            except Exception as e:
                no_data.append((year, ticker, f"query failed: {type(e).__name__}: {e}"))
                continue
            if stock_bounds is None:
                no_data.append((year, ticker, f"no market_prices data for {year}"))
                continue

            stock_return = compute_return(float(stock_bounds["start_close"]), float(stock_bounds["end_close"]))
            index_return = compute_return(float(index_bounds["start_close"]), float(index_bounds["end_close"]))
            abnormal = compute_abnormal_return(stock_return, index_return)
            results.append((year, ticker, stock_return, index_return, abnormal))

    print()
    print(f"{'Year':<6}{'Ticker':<10}{'Stock Return':>15}{'Index Return':>15}{'Abnormal Return':>18}")
    print("-" * 64)
    for year, ticker, stock_r, index_r, abn_r in sorted(results, key=lambda r: (r[0], r[1])):
        print(f"{year:<6}{ticker:<10}{stock_r * 100:>14.2f}%{index_r * 100:>14.2f}%{abn_r * 100:>17.2f}%")

    if no_data:
        print()
        print("NO DATA (never guessed/estimated):")
        for year, ticker, reason in no_data:
            print(f"  {year} {ticker}: {reason}")


if __name__ == "__main__":
    main()
