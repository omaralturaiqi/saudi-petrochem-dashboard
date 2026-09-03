#!/usr/bin/env python3
"""
scripts/classify_opportunities.py

READ-ONLY. No Neon writes of any kind — every query in this file is a
SELECT. Classifies companies that already have real data in
core.financial_line_items (NOT all 253 Tadawul companies — only the ones
with an actual earnings-trend signal) into one of 5 buckets by crossing
their net-income direction against their price-based Abnormal Return over
the same period.

WHAT THIS SCRIPT DOES:
  1. Reads core.financial_line_items for every company that has at least
     one row there (fetch_companies_with_financials()) — as of this
     script's writing that's SABIC/YANSAB/Advanced plus however many of
     ALBABTAIN/EIC/ABO MOATI have actually been inserted via
     confirmed_financials_batch1.sql and any follow-up batches; this
     script does not hardcode that list, it reads it live.
  2. For each such company, reads every net_income_attributable_to_parent
     / net_income row (fetch_net_income_rows()) and computes an earnings
     trend (compute_earnings_trend()): prefers
     net_income_attributable_to_parent over net_income whenever a company
     has BOTH (the more precise, minority-interest-excluded figure), then
     compares the two most recent distinct fiscal years' values for
     whichever concept was chosen:
       UP      — latest value > previous value
       DOWN    — latest value < previous value
       UNCLEAR — only one fiscal year available for the chosen concept
                 (or the two most recent values are exactly equal — not
                 addressed by this task's own UP/DOWN wording, so treated
                 conservatively as UNCLEAR rather than guessed either way)
  3. For each company, computes price-based Abnormal Return
     (fetch_abnormal_return_for_year()) for the SAME fiscal year as its
     latest available earnings data — REUSING
     market_analysis_api.SEARCH_HISTORY_SQL directly (imported, not
     duplicated — per this task's own "أعد استخدامه إن أمكن معماريًا"
     instruction), the same query already built and offline-tested for
     the Market Analysis tab's company-search feature. The result is
     filtered in Python to an EXACT ticker match (not just trusting
     SEARCH_HISTORY_SQL's own ILIKE '%ticker%' pattern-match, which is
     safe in practice for this project's all-4-digit tickers but is
     double-checked here anyway rather than assumed) and the requested
     fiscal_year.
  4. classify() applies the exact 5-way matrix this task specified:
       UP   + abnormal_return >0  -> ALREADY_PRICED_IN
       UP   + abnormal_return<=0  -> POTENTIAL_OPPORTUNITY
       DOWN + abnormal_return >0  -> MOMENTUM_RISK
       DOWN + abnormal_return<=0  -> CONSISTENT_DECLINE
       (trend UNCLEAR, OR abnormal_return could not be computed at all
        for that year — e.g. <150 trading days of price data, or no TASI
        data that year) -> INSUFFICIENT_DATA. This is a disclosed
       EXTENSION of the task's own INSUFFICIENT_DATA case (which only
       named the "UNCLEAR trend" scenario) to also cover a missing
       abnormal_return, since the given matrix has no defined bucket for
       "a real UP/DOWN trend but no computable Abnormal Return" and
       silently forcing that into ALREADY_PRICED_IN/POTENTIAL_OPPORTUNITY
       via an assumed abnormal_return sign would be exactly the kind of
       fabrication this project avoids. The printed table's
       abnormal_return_pct column shows "N/A" in that case, so the
       reason is visible, not hidden behind the same label as a genuine
       UNCLEAR-trend row.
  5. Prints one table: ticker, company, earnings_trend,
     abnormal_return_pct, classification — for companies with financial
     data ONLY. The other ~249/253 companies are never guessed at or
     listed with an invented trend.

WHAT THIS SCRIPT DELIBERATELY DOES NOT DO:
  - Never writes to Neon — every function here issues SELECT only.
  - Never classifies a company with no core.financial_line_items rows at
    all — per this task's explicit "لا تُخمِّن لباقي الـ253 بلا بيانات"
    instruction.
  - Never assumes a sign for a missing Abnormal Return — see point 4.
  - Never re-derives the Abnormal Return SQL from scratch — imports and
    reuses market_analysis_api.SEARCH_HISTORY_SQL directly.

USAGE:
    python3 scripts/classify_opportunities.py
        # requires NEON_CONNECTION_STRING in the environment (read-only
        # lookups only — see get_neon_sql_url() below, same pattern as
        # every other script this session).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Same sys.path setup used by scripts/extract_from_discovered_reports.py
# and scripts/dry_run_extract.py, so `market_analysis_api` imports
# correctly whether this script is run directly or as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from market_analysis_api import SEARCH_HISTORY_SQL  # noqa: E402  (see module docstring point 3)

NEON_CONNECTION_STRING = os.environ.get("NEON_CONNECTION_STRING")

# Preference order for the earnings-trend concept: the more precise,
# minority-interest-excluded figure wins whenever a company has BOTH
# concepts recorded — never averaged, never both counted.
NET_INCOME_CONCEPTS_PREFERRED_ORDER = ("net_income_attributable_to_parent", "net_income")


def get_neon_sql_url() -> str:
    if not NEON_CONNECTION_STRING:
        raise RuntimeError(
            "NEON_CONNECTION_STRING environment variable is not set. This script is "
            "read-only but still requires it, same as every other query script this session."
        )
    host = NEON_CONNECTION_STRING.split("@")[1].split("/")[0]
    return f"https://{host}/sql"


def run_query(sql: str, params: list | None = None) -> list[dict]:
    """Read-only helper — every call site in this script issues SELECT
    only, never INSERT/UPDATE/DELETE/DDL."""
    import requests

    body = {"query": sql}
    if params is not None:
        body["params"] = params
    resp = requests.post(
        get_neon_sql_url(),
        headers={"Neon-Connection-String": NEON_CONNECTION_STRING, "Content-Type": "application/json"},
        json=body, timeout=20,
    )
    resp.raise_for_status()
    return resp.json().get("rows", [])


def fetch_companies_with_financials() -> list[dict]:
    """Read-only. Only companies with >=1 row in core.financial_line_items
    — never all 253, per this task's explicit requirement."""
    return run_query(
        "SELECT DISTINCT c.ticker, c.name_en, c.name_ar "
        "FROM core.financial_line_items fli "
        "JOIN core.companies c ON c.company_id = fli.company_id "
        "ORDER BY c.ticker;"
    )


def fetch_net_income_rows(ticker: str | None = None) -> list[dict]:
    """Read-only. All net_income_attributable_to_parent / net_income rows
    for every company that has any — grouped/analyzed in Python by
    compute_earnings_trend(), not in SQL, so the concept-preference logic
    stays in one testable place.

    ticker: optional filter to a single company (added so this function
    is reusable for a single-company deep-dive view, e.g.
    market_analysis_api.py's per-company page, without fetching all
    253 companies' rows just to look up one). Default None preserves
    this function's original all-companies behavior exactly — every
    existing caller (this script's own main()) is unaffected."""
    # NET_INCOME_CONCEPTS_PREFERRED_ORDER's two values are fixed literals
    # this script controls (not user input), so they're inlined directly
    # rather than passed as a $1 array param — Neon's SQL-over-HTTP
    # endpoint's support for an ANY($1)-style array binding is untested
    # elsewhere in this codebase; a plain IN (...) literal is the
    # established, already-proven pattern (see every other run_query()
    # call in this project).
    concepts_sql_list = ", ".join(f"'{c}'" for c in NET_INCOME_CONCEPTS_PREFERRED_ORDER)
    sql = (
        "SELECT c.ticker, fli.concept, fli.fiscal_year, fli.value_raw "
        "FROM core.financial_line_items fli "
        "JOIN core.companies c ON c.company_id = fli.company_id "
        f"WHERE fli.concept IN ({concepts_sql_list})"
    )
    params = None
    if ticker is not None:
        sql += " AND c.ticker = $1"
        params = [ticker]
    sql += " ORDER BY c.ticker, fli.concept, fli.fiscal_year;"
    return run_query(sql, params)


def compute_earnings_trend(rows_for_ticker: list[dict]) -> tuple[str, int | None, str | None]:
    """Pure, offline-testable. rows_for_ticker: list of
    {"concept": ..., "fiscal_year": ..., "value_raw": ...} for ONE ticker
    (any mix of the two net-income concepts, any order). Returns
    (trend, latest_fiscal_year, concept_used):
      trend in {"UP", "DOWN", "UNCLEAR"}
      latest_fiscal_year: the most recent fiscal_year found for
        concept_used, or None if rows_for_ticker is empty entirely.
      concept_used: whichever of NET_INCOME_CONCEPTS_PREFERRED_ORDER was
        actually chosen (the first one with >=1 row), or None if neither
        concept has any row.
    """
    if not rows_for_ticker:
        return "UNCLEAR", None, None

    by_concept: dict[str, dict[int, float]] = {}
    for r in rows_for_ticker:
        by_concept.setdefault(r["concept"], {})[int(r["fiscal_year"])] = float(r["value_raw"])

    concept_used = next((c for c in NET_INCOME_CONCEPTS_PREFERRED_ORDER if by_concept.get(c)), None)
    if concept_used is None:
        return "UNCLEAR", None, None

    years = sorted(by_concept[concept_used].keys(), reverse=True)
    latest_year = years[0]
    if len(years) < 2:
        return "UNCLEAR", latest_year, concept_used

    latest_value = by_concept[concept_used][years[0]]
    previous_value = by_concept[concept_used][years[1]]
    if latest_value > previous_value:
        return "UP", latest_year, concept_used
    if latest_value < previous_value:
        return "DOWN", latest_year, concept_used
    return "UNCLEAR", latest_year, concept_used  # exactly equal — not guessed either way


def fetch_abnormal_return_for_year(ticker: str, fiscal_year: int) -> float | None:
    """Reuses market_analysis_api.SEARCH_HISTORY_SQL directly (see module
    docstring point 3) rather than re-deriving the Abnormal Return query.
    Filters the (potentially multi-year, potentially multi-company if the
    ILIKE pattern loosely matched something else) result to an EXACT
    ticker match and the requested fiscal_year — belt-and-suspenders on
    top of SEARCH_HISTORY_SQL's own pattern match. Returns None if no
    matching row exists (e.g. <150 trading days of price data that year,
    or no TASI data that year) — never a guessed value."""
    like_pattern = f"%{ticker}%"
    rows = run_query(SEARCH_HISTORY_SQL, [like_pattern])
    for r in rows:
        if r["ticker"] == ticker and int(r["fiscal_year"]) == fiscal_year:
            return float(r["abnormal_return_pct"]) if r["abnormal_return_pct"] is not None else None
    return None


def classify(trend: str, abnormal_return_pct: float | None) -> str:
    """Pure, offline-testable. Implements this task's exact 5-way matrix,
    plus the disclosed extension (see module docstring point 4): a real
    UP/DOWN trend with no computable abnormal_return is ALSO
    INSUFFICIENT_DATA, not force-classified into a wrong bucket."""
    if trend == "UNCLEAR" or abnormal_return_pct is None:
        return "INSUFFICIENT_DATA"
    if trend == "UP":
        return "ALREADY_PRICED_IN" if abnormal_return_pct > 0 else "POTENTIAL_OPPORTUNITY"
    if trend == "DOWN":
        return "MOMENTUM_RISK" if abnormal_return_pct > 0 else "CONSISTENT_DECLINE"
    raise ValueError(f"unexpected trend value: {trend!r}")  # should be unreachable given the 3 known trend values


def main() -> None:
    print("=" * 96)
    print("CLASSIFY OPPORTUNITIES — earnings trend x Abnormal Return, companies WITH financial data only")
    print("READ-ONLY: no writes issued against Neon anywhere in this script.")
    print("=" * 96)

    try:
        companies = fetch_companies_with_financials()
    except Exception as e:
        print(f"FAILED to read core.financial_line_items: {type(e).__name__}: {e}")
        sys.exit(1)

    if not companies:
        print("No companies with any core.financial_line_items rows yet — nothing to classify.")
        return

    try:
        all_net_income_rows = fetch_net_income_rows()
    except Exception as e:
        print(f"FAILED to read net-income rows: {type(e).__name__}: {e}")
        sys.exit(1)

    rows_by_ticker: dict[str, list[dict]] = {}
    for r in all_net_income_rows:
        rows_by_ticker.setdefault(r["ticker"], []).append(r)

    results = []
    for c in companies:
        ticker = c["ticker"]
        trend, latest_year, concept_used = compute_earnings_trend(rows_by_ticker.get(ticker, []))

        abnormal_return_pct = None
        if latest_year is not None:
            try:
                abnormal_return_pct = fetch_abnormal_return_for_year(ticker, latest_year)
            except Exception as e:
                print(f"  {ticker}: WARNING — failed to fetch Abnormal Return for FY{latest_year}: "
                      f"{type(e).__name__}: {e}")

        classification = classify(trend, abnormal_return_pct)
        results.append({
            "ticker": ticker,
            "company": c.get("name_en") or c.get("name_ar") or "—",
            "earnings_trend": trend,
            "latest_year": latest_year,
            "concept_used": concept_used,
            "abnormal_return_pct": abnormal_return_pct,
            "classification": classification,
        })

    print()
    print(f"{'Ticker':<8}{'Company':<28}{'Trend':<10}{'FY':<7}{'AbnormalRet%':<14}{'Classification'}")
    print("-" * 96)
    for r in results:
        ar_display = f"{r['abnormal_return_pct']:.1f}%" if r["abnormal_return_pct"] is not None else "N/A"
        fy_display = str(r["latest_year"]) if r["latest_year"] is not None else "—"
        print(f"{r['ticker']:<8}{r['company'][:26]:<28}{r['earnings_trend']:<10}{fy_display:<7}{ar_display:<14}{r['classification']}")

    print("=" * 96)
    print(f"Companies classified: {len(results)} (companies with core.financial_line_items data only)")
    print("NEON WRITES ISSUED BY THIS SCRIPT: 0")
    print("=" * 96)


if __name__ == "__main__":
    main()
