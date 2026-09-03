"""
market_analysis_api.py

Namespace: /market-analysis/*  (isolated from existing Saudi routes: /, /health,
and from /us-xbrl/*). Uses SQL-over-HTTP via `requests`
(NEON_CONNECTION_STRING -> NEON_SQL_URL), the SAME connection pattern as
us_xbrl_api.py and app.py — cloned here rather than imported, because
us_xbrl_api.py itself is self-contained (defines its own
NEON_CONNECTION_STRING/NEON_SQL_URL/run_query rather than importing
app.py's), so this blueprint follows that exact established precedent
instead of introducing a new cross-module import app.py doesn't already
have.

Bilingual UI pattern (UI_STRINGS / lang query param / RTL isolation CSS)
is copied literally from us_xbrl_api.py's DASHBOARD_TEMPLATE — same
lang-switch markup, same CSS class names, same "lang is validated against
an explicit allow-list and never touches SQL" rule, same "translation is
presentation-only — never mutates a returned value" rule (see
pick_display_name() below: it prefers name_ar/name_en depending on the
current language ONLY from what the row already carries; it never
constructs a translated string that isn't already in the row).

DATA — read this before trusting the output:
  core.market_prices and core.market_indices (TASI) are reportedly
  populated (701,779 rows / 253 companies and 3,683 TASI days, per the
  task this file was built from) — not independently re-verified by
  Claude in this authoring session (no live Neon access here; see
  scripts/*.py's own repeated "NEON ACCESS = NOT AVAILABLE" notes this
  session). Abnormal Return here is PRICE-ONLY (stock return - TASI
  index return over the same calendar year) — it does not depend on
  core.financial_line_items at all, so a company search shows every
  year with enough price data (HAVING COUNT(*) >= 150, matching the
  leaderboard query's own threshold) regardless of whether that
  company's fundamentals (net income, revenue, ...) have been entered
  yet. This is stated explicitly in the page's subtitle, not just this
  comment, so it's visible to whoever is looking at the numbers.

DEEP-DIVE (this revision): a company search result is no longer just the
multi-year Abnormal Return table — build_deep_dive() adds, per matched
ticker: (a) its opportunity classification, ONLY if
core.financial_line_items has any data for it — reuses
scripts.classify_opportunities.compute_earnings_trend()/classify()
directly via a LOCAL (function-scope) import rather than reimplementing
that logic; (b) the officially-cited "why" text (that concept/year's
review_notes, falling back to reported_label) shown verbatim with an
explicit "officially announced by the company itself" attribution, never
reworded or interpreted; (c) historical "sharp move" cycles — every year
with |Abnormal Return| > 15%, each honestly flagged whether ANY financial
data exists for that specific year (never an invented explanation when
it doesn't); (d) a fixed bilingual disclaimer under every deep-dive block
stating the classification is not a timing prediction and that most
companies have no financial data yet. A company with zero
financial_line_items rows shows ONLY the historical-cycles section plus
an explicit "no confirmed financial data yet" message — never a silently
missing section, never a guessed classification.

WHAT THIS FILE DOES NOT DO:
  - Never writes to Neon — every query here is a read-only SELECT.
  - Never touches app.py's or us_xbrl_api.py's own routes, templates, or
    run_query() — this blueprint is fully self-contained, same as
    us_xbrl_api.py is relative to app.py.
  - Never fabricates a translated company name — see pick_display_name().
  - Never reimplements scripts/classify_opportunities.py's own trend/
    classification logic — imports and reuses it directly.
  - Never invents a "why" explanation, or an explanation for a historical
    sharp-move year with no recorded financial data — both show an
    honest absence instead.
"""
from flask import Blueprint, render_template_string, request
import os
import requests

market_analysis_bp = Blueprint("market_analysis", __name__, url_prefix="/market-analysis")

NEON_CONNECTION_STRING = os.environ.get("NEON_CONNECTION_STRING", "<PRODUCTION_SECRET>")
NEON_HOST = NEON_CONNECTION_STRING.split("@")[1].split("/")[0] if "@" in NEON_CONNECTION_STRING else None
NEON_SQL_URL = f"https://{NEON_HOST}/sql" if NEON_HOST else None


def run_query(sql, params=None):
    """Identical pattern to us_xbrl_api.py's run_query(): params is a list
    bound to $1, $2, ... placeholders in sql, passed through to Neon's
    SQL-over-HTTP endpoint as real parameterized query bindings (server-
    side, not client-side string escaping)."""
    body = {"query": sql}
    if params is not None:
        body["params"] = params
    resp = requests.post(
        NEON_SQL_URL,
        headers={"Neon-Connection-String": NEON_CONNECTION_STRING, "Content-Type": "application/json"},
        json=body, timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("rows", [])


# Verbatim — this is the query already manually tested this session for
# the 2025 leaderboard. NOT re-derived or altered in any way (including
# whitespace-insignificant formatting) from what was tested.
LEADERBOARD_SQL_2025 = """
WITH yearly_stock AS (
    SELECT c.ticker, c.name_en, c.name_ar, c.sector,
        (ARRAY_AGG(mp.close_price ORDER BY mp.trade_date ASC))[1] AS price_start,
        (ARRAY_AGG(mp.close_price ORDER BY mp.trade_date DESC))[1] AS price_end
    FROM core.market_prices mp
    JOIN core.companies c ON c.company_id = mp.company_id
    WHERE EXTRACT(YEAR FROM mp.trade_date) = 2025
    GROUP BY c.ticker, c.name_en, c.name_ar, c.sector
    HAVING COUNT(*) >= 150
),
yearly_index AS (
    SELECT (ARRAY_AGG(close_value ORDER BY trade_date ASC))[1] AS idx_start,
           (ARRAY_AGG(close_value ORDER BY trade_date DESC))[1] AS idx_end
    FROM core.market_indices
    WHERE index_code = 'TASI' AND EXTRACT(YEAR FROM trade_date) = 2025
)
SELECT ys.ticker, ys.name_en, ys.name_ar, ys.sector,
    ROUND(100.0*(ys.price_end-ys.price_start)/ys.price_start, 1) AS stock_return_pct,
    ROUND(100.0*(yi.idx_end-yi.idx_start)/yi.idx_start, 1) AS index_return_pct,
    ROUND(100.0*(ys.price_end-ys.price_start)/ys.price_start
          - 100.0*(yi.idx_end-yi.idx_start)/yi.idx_start, 1) AS abnormal_return_pct
FROM yearly_stock ys CROSS JOIN yearly_index yi
ORDER BY abnormal_return_pct DESC;
"""

# NEW for this task (the leaderboard query above only covers a single,
# hardcoded year — 2025 — so it cannot serve a company's full history).
# Generalizes that SAME query's shape (ARRAY_AGG first/last close price
# per grouping, the identical HAVING COUNT(*) >= 150 threshold, the
# identical return-percentage formula) to be PER-YEAR and PER-SEARCH-
# MATCH instead of hardcoded to one year and all companies: the company
# match (by ticker/name_en/name_ar, case-insensitive, partial) is done
# once in matched_companies, then joined against every calendar year
# that company has enough price data for, so a search shows the
# company's full available price-based Abnormal Return history — see
# module docstring's "DATA" section for why this is price-only and
# does not depend on core.financial_line_items being populated.
SEARCH_HISTORY_SQL = """
WITH matched_companies AS (
    SELECT company_id, ticker, name_en, name_ar, sector
    FROM core.companies
    WHERE ticker ILIKE $1 OR name_en ILIKE $1 OR name_ar ILIKE $1
),
yearly_stock AS (
    SELECT mc.ticker, mc.name_en, mc.name_ar, mc.sector,
        EXTRACT(YEAR FROM mp.trade_date)::int AS fiscal_year,
        (ARRAY_AGG(mp.close_price ORDER BY mp.trade_date ASC))[1] AS price_start,
        (ARRAY_AGG(mp.close_price ORDER BY mp.trade_date DESC))[1] AS price_end
    FROM core.market_prices mp
    JOIN matched_companies mc ON mc.company_id = mp.company_id
    GROUP BY mc.ticker, mc.name_en, mc.name_ar, mc.sector, EXTRACT(YEAR FROM mp.trade_date)
    HAVING COUNT(*) >= 150
),
yearly_index AS (
    SELECT EXTRACT(YEAR FROM trade_date)::int AS fiscal_year,
           (ARRAY_AGG(close_value ORDER BY trade_date ASC))[1] AS idx_start,
           (ARRAY_AGG(close_value ORDER BY trade_date DESC))[1] AS idx_end
    FROM core.market_indices
    WHERE index_code = 'TASI'
    GROUP BY EXTRACT(YEAR FROM trade_date)
)
SELECT ys.ticker, ys.name_en, ys.name_ar, ys.sector, ys.fiscal_year,
    ROUND(100.0*(ys.price_end-ys.price_start)/ys.price_start, 1) AS stock_return_pct,
    ROUND(100.0*(yi.idx_end-yi.idx_start)/yi.idx_start, 1) AS index_return_pct,
    ROUND(100.0*(ys.price_end-ys.price_start)/ys.price_start
          - 100.0*(yi.idx_end-yi.idx_start)/yi.idx_start, 1) AS abnormal_return_pct
FROM yearly_stock ys
JOIN yearly_index yi ON yi.fiscal_year = ys.fiscal_year
ORDER BY ys.ticker, ys.fiscal_year DESC;
"""


def pick_display_name(row: dict, lang: str) -> str | None:
    """Pure, offline-testable. Presentation-only: picks which of the
    row's OWN name_en/name_ar to show for the current language — never
    constructs, translates, or guesses a name that isn't already present
    in the row. Prefers the language-matching field; falls back to
    whichever of the two is actually present if the preferred one is
    missing; returns None (rendered as '—' by the template) only if
    BOTH are missing."""
    preferred = row.get("name_ar") if lang == "ar" else row.get("name_en")
    fallback = row.get("name_en") if lang == "ar" else row.get("name_ar")
    return preferred or fallback


def fetch_financial_line_items_for_ticker(ticker: str) -> list[dict]:
    """Read-only. ALL core.financial_line_items rows for one ticker (every
    concept, not just net_income) — used by build_deep_dive() for the
    "why" text (review_notes/reported_label) and to check whether a given
    year has ANY confirmed financial data at all (for the historical-
    cycles table's "financial data available?" column). Self-contained
    within this blueprint, matching its own established run_query()
    pattern."""
    return run_query(
        "SELECT fli.concept, fli.fiscal_year, fli.value_raw, fli.unit, "
        "fli.reported_label, fli.review_notes, fli.confidence "
        "FROM core.financial_line_items fli "
        "JOIN core.companies c ON c.company_id = fli.company_id "
        "WHERE c.ticker = $1 "
        "ORDER BY fli.fiscal_year DESC;",
        [ticker],
    )


# Abnormal Return magnitude above which a year counts as a "sharp move" /
# historical cycle worth surfacing separately — per this task's own
# explicit >+15% or <-15% threshold.
HISTORICAL_CYCLE_THRESHOLD_PCT = 15.0


def build_deep_dive(ticker: str, history_rows: list[dict]) -> dict:
    """Builds one company's full deep-dive block:
      - classification (earnings trend x Abnormal Return), ONLY if the
        company has any core.financial_line_items data at all — reuses
        scripts.classify_opportunities.compute_earnings_trend()/classify()
        directly (imported, not reimplemented — see the local import
        below for why it's local, not module-level).
      - the officially-cited "why" text (review_notes, falling back to
        reported_label) for the specific (concept, fiscal_year) row the
        classification's earnings trend was actually computed from — never
        a different row, never reworded.
      - historical sharp-move cycles: every year in history_rows with
        |Abnormal Return| > HISTORICAL_CYCLE_THRESHOLD_PCT, each honestly
        flagged Yes/No for whether ANY financial_line_items row exists for
        that specific year — never an invented explanation when the
        answer is No.

    history_rows: this ticker's own slice of a SEARCH_HISTORY_SQL result
    (already fetched by the caller for the existing multi-year table) —
    REUSED here for the classification's Abnormal Return figure too,
    rather than calling scripts.classify_opportunities.
    fetch_abnormal_return_for_year() again, which would re-run the exact
    same query a second time for data already in memory."""
    financial_rows = fetch_financial_line_items_for_ticker(ticker)
    has_financial_data = bool(financial_rows)

    classification_info = None
    why_text = None
    if has_financial_data:
        # Local (function-scope) import, not module-level: avoids a
        # circular import at MODULE LOAD time, since
        # scripts/classify_opportunities.py itself does
        # `from market_analysis_api import SEARCH_HISTORY_SQL` at its own
        # module level. By the time this function is first called (a real
        # HTTP request), market_analysis_api.py has already fully finished
        # loading (app.py imports it before any request can arrive), so
        # classify_opportunities.py's reverse import succeeds cleanly the
        # first time IT loads, here.
        from scripts.classify_opportunities import classify, compute_earnings_trend, fetch_net_income_rows

        net_income_rows = fetch_net_income_rows(ticker)
        trend, latest_year, concept_used = compute_earnings_trend(net_income_rows)

        abnormal_return_pct = None
        if latest_year is not None:
            match = next((r for r in history_rows if r.get("fiscal_year") == latest_year), None)
            if match is not None and match.get("abnormal_return_pct") is not None:
                abnormal_return_pct = float(match["abnormal_return_pct"])

        classification_value = classify(trend, abnormal_return_pct)
        classification_info = {
            "trend": trend, "latest_year": latest_year,
            "abnormal_return_pct": abnormal_return_pct, "classification": classification_value,
        }

        if concept_used is not None and latest_year is not None:
            why_row = next(
                (r for r in financial_rows if r["concept"] == concept_used and int(r["fiscal_year"]) == latest_year),
                None,
            )
            if why_row:
                why_text = why_row.get("review_notes") or why_row.get("reported_label")

    financial_years = {int(r["fiscal_year"]) for r in financial_rows}
    historical_cycles = []
    for r in history_rows:
        ar = r.get("abnormal_return_pct")
        if ar is None:
            continue
        ar = float(ar)
        if abs(ar) > HISTORICAL_CYCLE_THRESHOLD_PCT:
            historical_cycles.append({
                "fiscal_year": r["fiscal_year"],
                "abnormal_return_pct": ar,
                "has_financial_data": r["fiscal_year"] in financial_years,
            })

    return {
        "ticker": ticker,
        "display_name": history_rows[0].get("display_name") if history_rows else ticker,
        "sector": history_rows[0].get("sector") if history_rows else None,
        "has_financial_data": has_financial_data,
        "classification_info": classification_info,
        "why_text": why_text,
        "historical_cycles": historical_cycles,
        "history_rows": history_rows,
    }


def split_leaderboard(rows: list[dict], limit: int = 10) -> tuple[list[dict], list[dict]]:
    """Pure, offline-testable. `rows` must already be ORDER BY
    abnormal_return_pct DESC (as LEADERBOARD_SQL_2025 itself guarantees) —
    this function does not re-sort. top = the first `limit` rows as-is;
    worst = the SAME result set reversed, then the first `limit` of that
    — i.e. exactly "the same result, order reversed, LIMIT 10" as this
    task specified, computed once from one fetched result set, not a
    second query."""
    top = rows[:limit]
    worst = list(reversed(rows))[:limit]
    return top, worst


UI_STRINGS = {
    "en": {
        "html_lang": "en", "html_dir": "ltr",
        "page_title": "Market Analysis — Abnormal Return",
        "heading": "Market Analysis",
        "badge": "Read-only — live query",
        "subtitle": "Abnormal Return = Stock Return − TASI Index Return, price-only (does not require "
                     "core.financial_line_items to be populated for that company/year).",
        "lang_switch_ar": "العربية", "lang_switch_en": "English",
        "top_heading": "Top Performers (Abnormal Return)",
        "worst_heading": "Worst Performers (Abnormal Return)",
        "search_heading": "Search for a company",
        "search_placeholder": "Ticker or company name",
        "search_button": "Search",
        "search_results_heading": "Search results",
        "no_results": "No company found",
        "no_data": "No data available.",
        "th_ticker": "Ticker", "th_company": "Company", "th_sector": "Sector",
        "th_stock_return": "Stock Return", "th_index_return": "Index Return (TASI)",
        "th_abnormal_return": "Abnormal Return", "th_fiscal_year": "Fiscal Year",
        "classification_labels": {
            "ALREADY_PRICED_IN": "Already Priced In", "POTENTIAL_OPPORTUNITY": "Potential Opportunity",
            "MOMENTUM_RISK": "Momentum Risk", "CONSISTENT_DECLINE": "Consistent Decline",
            "INSUFFICIENT_DATA": "Insufficient Data",
        },
        "trend_labels": {"UP": "Earnings Up", "DOWN": "Earnings Down", "UNCLEAR": "Unclear"},
        "why_heading": "Why", "why_attribution": "Officially announced reason from the company itself",
        "cycles_heading": "Historical Cycles (moves > ±15%)",
        "cycles_th_year": "Year", "cycles_th_magnitude": "Move Size", "cycles_th_has_data": "Financial Data Available?",
        "yes": "Yes", "no": "No",
        "no_financial_data": "No confirmed financial data available for this company yet — full "
                              "analysis is not currently possible.",
        "disclaimer": "This classification relies only on the financial and price data currently "
                       "available, and is not a prediction of the timing of any future price "
                       "movement. Financial data is not currently available for most companies — "
                       "full classification is only possible for companies with confirmed "
                       "financial_line_items data.",
        "footer_1": "Figures are computed live from core.market_prices and core.market_indices "
                     "(TASI, index_code='TASI') — no caching.",
        "footer_2": "Year-start/year-end close prices, requires at least 150 trading days of price "
                     "data in a calendar year to be included (matches this table's own query threshold).",
    },
    "ar": {
        "html_lang": "ar", "html_dir": "rtl",
        "page_title": "تحليل السوق — العائد الشاذ",
        "heading": "تحليل السوق",
        "badge": "للقراءة فقط — استعلام حي",
        "subtitle": "العائد الشاذ = عائد السهم − عائد مؤشر تاسي، سعري بحت (لا يتطلب توفر بيانات "
                     "core.financial_line_items لهذه الشركة/السنة).",
        "lang_switch_ar": "العربية", "lang_switch_en": "English",
        "top_heading": "الأفضل أداءً (العائد الشاذ)",
        "worst_heading": "الأسوأ أداءً (العائد الشاذ)",
        "search_heading": "البحث عن شركة",
        "search_placeholder": "الرمز أو اسم الشركة",
        "search_button": "بحث",
        "search_results_heading": "نتيجة البحث",
        "no_results": "لم يُعثر على الشركة",
        "no_data": "لا توجد بيانات متاحة.",
        "th_ticker": "الرمز", "th_company": "الشركة", "th_sector": "القطاع",
        "th_stock_return": "عائد السهم", "th_index_return": "عائد المؤشر (تاسي)",
        "th_abnormal_return": "العائد الشاذ", "th_fiscal_year": "السنة المالية",
        "classification_labels": {
            "ALREADY_PRICED_IN": "مُسعَّر بالفعل", "POTENTIAL_OPPORTUNITY": "فرصة محتملة",
            "MOMENTUM_RISK": "مخاطرة زخم", "CONSISTENT_DECLINE": "تراجع مستمر",
            "INSUFFICIENT_DATA": "بيانات غير كافية",
        },
        "trend_labels": {"UP": "أرباح صاعدة", "DOWN": "أرباح هابطة", "UNCLEAR": "غير واضح"},
        "why_heading": "لماذا", "why_attribution": "السبب المُعلَن رسميًا من الشركة نفسها",
        "cycles_heading": "دورات تاريخية سابقة (تحركات > ±15%)",
        "cycles_th_year": "السنة", "cycles_th_magnitude": "حجم التحرك", "cycles_th_has_data": "تتوفر بيانات مالية؟",
        "yes": "نعم", "no": "لا",
        "no_financial_data": "لا تتوفر بيانات مالية مؤكَّدة لهذي الشركة بعد — التحليل الكامل غير ممكن حاليًا",
        "disclaimer": "هذا التصنيف يعتمد فقط على البيانات المالية والسعرية المتوفرة حاليًا، وليس "
                       "تنبؤًا بتوقيت أي حركة سعرية مستقبلية. البيانات المالية غير متوفرة حاليًا "
                       "لمعظم الشركات — التصنيف الكامل ممكن فقط للشركات ذات بيانات "
                       "financial_line_items مؤكَّدة.",
        "footer_1": "الأرقام مُحتسَبة مباشرة من core.market_prices وcore.market_indices "
                     "(تاسي، index_code='TASI') — بدون تخزين مؤقت.",
        "footer_2": "أسعار الإغلاق أول/آخر يوم بالسنة، بشرط 150 يوم تداول على الأقل ضمن السنة "
                     "(نفس حد الاستعلام في هذا الجدول).",
    },
}


DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="{{ t.html_lang }}" dir="{{ t.html_dir }}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ t.page_title }}</title>
<style>
  body { font-family: "Segoe UI", Tahoma, sans-serif; background:#0b0d12; color:#e8e8ea; margin:0; padding:24px; }
  h1 { font-size: 20px; color:#fff; margin-bottom:4px; }
  h2 { font-size:15px; color:#c8ccd4; margin-bottom:10px; }
  .subtitle { color:#8a8f98; font-size:13px; margin-bottom:24px; }
  .badge { display:inline-block; background:#1c2333; color:#7dd3fc; border-radius:4px; padding:2px 8px; font-size:11px; margin-right:6px; }
  table { width:100%; border-collapse: collapse; margin-bottom:32px; background:#12151c; border-radius:8px; overflow:hidden; }
  th, td { padding:10px 14px; text-align:left; border-bottom:1px solid #1e222c; font-size:13px; }
  th { background:#171b24; color:#9aa4b2; font-weight:600; font-size:12px; }
  tr:hover td { background:#161a23; }
  .neg { color:#f87171; }
  .pos { color:#4ade80; }
  .na { color:#5b6472; font-style:italic; }
  .empty { color:#5b6472; font-style:italic; padding:20px; text-align:center; }
  .footer { color:#5b6472; font-size:12px; margin-top:40px; border-top:1px solid #1e222c; padding-top:16px; }
  section { margin-bottom:40px; }
  .lang-switch { float:right; font-size:13px; margin-bottom:16px; }
  .lang-switch a { color:#8a8f98; text-decoration:none; margin-left:8px; }
  .lang-switch a.active { color:#7dd3fc; font-weight:600; }
  [dir="rtl"] .lang-switch { float:left; }
  [dir="rtl"] .lang-switch a { margin-left:0; margin-right:8px; }
  [dir="rtl"] .ticker,
  [dir="rtl"] .numeric,
  [dir="rtl"] .fiscal-year {
    direction: ltr;
    unicode-bidi: isolate;
    display: inline-block;
  }
  .search-form { background:#12151c; border-radius:8px; padding:16px; margin-bottom:16px; }
  .search-form input[type="text"] { background:#0b0d12; border:1px solid #1e222c; color:#e8e8ea; padding:8px 12px; border-radius:4px; font-size:13px; min-width:240px; }
  .search-form button { background:#1c2333; color:#7dd3fc; border:1px solid #2a3346; padding:8px 16px; border-radius:4px; font-size:13px; cursor:pointer; margin-left:8px; }
  [dir="rtl"] .search-form button { margin-left:0; margin-right:8px; }
  .deep-dive { background:#0e1116; border:1px solid #1e222c; border-radius:8px; padding:20px; margin-bottom:24px; }
  .deep-dive h3 { font-size:16px; color:#fff; margin:0 0 12px 0; }
  .deep-dive h4 { font-size:13px; color:#9aa4b2; margin:20px 0 8px 0; }
  .classification-block { margin-bottom:12px; }
  .cls-badge { display:inline-block; border-radius:4px; padding:4px 10px; font-size:12px; font-weight:600; margin-right:8px; }
  .cls-badge-ALREADY_PRICED_IN { background:#1c2c3f; color:#7dd3fc; }
  .cls-badge-POTENTIAL_OPPORTUNITY { background:#16301f; color:#4ade80; }
  .cls-badge-MOMENTUM_RISK { background:#3a2a12; color:#fbbf24; }
  .cls-badge-CONSISTENT_DECLINE { background:#3a1717; color:#f87171; }
  .cls-badge-INSUFFICIENT_DATA { background:#22242b; color:#8a8f98; }
  .trend-badge { font-size:12px; color:#9aa4b2; margin-right:8px; }
  [dir="rtl"] .cls-badge, [dir="rtl"] .trend-badge { margin-right:0; margin-left:8px; }
  .why-block { background:#12151c; border-radius:6px; padding:12px 14px; margin-bottom:16px; font-size:13px; }
  .why-block p { margin:6px 0 0 0; color:#c8ccd4; white-space:pre-wrap; }
  .disclaimer { background:#171412; border:1px solid #2e2418; color:#c9a876; border-radius:6px; padding:12px 14px; font-size:12px; margin:16px 0; }
</style>
</head>
<body>
  <div class="lang-switch">
    <a href="?lang=ar{{ q_suffix }}" class="{{ 'active' if lang=='ar' else '' }}">{{ t.lang_switch_ar }}</a>
    <a href="?lang=en{{ q_suffix }}" class="{{ 'active' if lang=='en' else '' }}">{{ t.lang_switch_en }}</a>
  </div>
  <h1>{{ t.heading }} <span class="badge">{{ t.badge }}</span></h1>
  <div class="subtitle">{{ t.subtitle }}</div>

  {% macro return_table(rows) %}
    {% if rows %}
    <table>
      <tr>
        <th>{{ t.th_ticker }}</th><th>{{ t.th_company }}</th><th>{{ t.th_sector }}</th>
        {% if rows[0].fiscal_year is defined %}<th>{{ t.th_fiscal_year }}</th>{% endif %}
        <th>{{ t.th_stock_return }}</th><th>{{ t.th_index_return }}</th><th>{{ t.th_abnormal_return }}</th>
      </tr>
      {% for r in rows %}
      <tr>
        <td><b class="ticker">{{ r.ticker }}</b></td>
        <td>{{ r.display_name or '—' }}</td>
        <td>{{ r.sector or '—' }}</td>
        {% if r.fiscal_year is defined %}<td class="fiscal-year">{{ r.fiscal_year }}</td>{% endif %}
        {% if r.stock_return_pct is not none %}
        <td class="numeric {{ 'neg' if r.stock_return_pct|float < 0 else 'pos' }}">{{ r.stock_return_pct }}%</td>
        {% else %}<td class="na">—</td>{% endif %}
        {% if r.index_return_pct is not none %}
        <td class="numeric {{ 'neg' if r.index_return_pct|float < 0 else 'pos' }}">{{ r.index_return_pct }}%</td>
        {% else %}<td class="na">—</td>{% endif %}
        {% if r.abnormal_return_pct is not none %}
        <td class="numeric {{ 'neg' if r.abnormal_return_pct|float < 0 else 'pos' }}">{{ r.abnormal_return_pct }}%</td>
        {% else %}<td class="na">—</td>{% endif %}
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="empty">{{ t.no_data }}</div>
    {% endif %}
  {% endmacro %}

  <section>
    <h2>{{ t.top_heading }}</h2>
    {{ return_table(top10) }}
  </section>

  <section>
    <h2>{{ t.worst_heading }}</h2>
    {{ return_table(worst10) }}
  </section>

  <section>
    <h2>{{ t.search_heading }}</h2>
    <form class="search-form" method="GET">
      <input type="hidden" name="lang" value="{{ lang }}">
      <input type="text" name="q" value="{{ q or '' }}" placeholder="{{ t.search_placeholder }}">
      <button type="submit">{{ t.search_button }}</button>
    </form>
    {% if q %}
      <h2>{{ t.search_results_heading }}: <span class="ticker">{{ q }}</span></h2>
      {% if deep_dives %}
        {% for dd in deep_dives %}
        <div class="deep-dive">
          <h3>{{ dd.display_name or dd.ticker }} <span class="ticker">({{ dd.ticker }})</span>{% if dd.sector %} — {{ dd.sector }}{% endif %}</h3>

          {% if dd.has_financial_data %}
            {% set ci = dd.classification_info %}
            <div class="classification-block">
              <span class="cls-badge cls-badge-{{ ci.classification }}">{{ t.classification_labels.get(ci.classification, ci.classification) }}</span>
              <span class="trend-badge">{{ t.trend_labels.get(ci.trend, ci.trend) }}{% if ci.latest_year %} (FY{{ ci.latest_year }}){% endif %}</span>
            </div>
            {% if dd.why_text %}
            <div class="why-block">
              <strong>{{ t.why_heading }}</strong> — <em>{{ t.why_attribution }}</em>
              <p>{{ dd.why_text }}</p>
            </div>
            {% endif %}
          {% else %}
            <div class="empty">{{ t.no_financial_data }}</div>
          {% endif %}

          <h4>{{ t.cycles_heading }}</h4>
          {% if dd.historical_cycles %}
          <table>
            <tr><th>{{ t.cycles_th_year }}</th><th>{{ t.cycles_th_magnitude }}</th><th>{{ t.cycles_th_has_data }}</th></tr>
            {% for cyc in dd.historical_cycles %}
            <tr>
              <td class="fiscal-year">{{ cyc.fiscal_year }}</td>
              <td class="numeric {{ 'neg' if cyc.abnormal_return_pct < 0 else 'pos' }}">{{ cyc.abnormal_return_pct }}%</td>
              <td>{{ t.yes if cyc.has_financial_data else t.no }}</td>
            </tr>
            {% endfor %}
          </table>
          {% else %}
            <div class="empty">{{ t.no_data }}</div>
          {% endif %}

          <div class="disclaimer">{{ t.disclaimer }}</div>

          {{ return_table(dd.history_rows) }}
        </div>
        {% endfor %}
      {% else %}
        <div class="empty">{{ t.no_results }}</div>
      {% endif %}
    {% endif %}
  </section>

  <div class="footer">
    {{ t.footer_1 }}<br>
    {{ t.footer_2 }}
  </div>
</body>
</html>
"""


@market_analysis_bp.route("/")
def market_analysis_dashboard():
    # Same allow-list validation pattern as us_xbrl_api.py's
    # us_xbrl_dashboard(): lang never touches SQL, safely falls back to
    # "en" for anything outside {"ar","en"}.
    lang = request.args.get("lang", "en")
    if lang not in ("ar", "en"):
        lang = "en"

    q = request.args.get("q", "").strip()
    q_suffix = f"&q={q}" if q else ""

    rows = run_query(LEADERBOARD_SQL_2025)
    for r in rows:
        r["display_name"] = pick_display_name(r, lang)
    top10, worst10 = split_leaderboard(rows)

    search_results = []
    deep_dives = []
    if q:
        like_pattern = f"%{q}%"
        # $1 is reused 3 times in SEARCH_HISTORY_SQL (standard PostgreSQL
        # extended-query-protocol behavior: a placeholder can appear
        # multiple times bound to one value) — one param, not three.
        search_results = run_query(SEARCH_HISTORY_SQL, [like_pattern])
        for r in search_results:
            r["display_name"] = pick_display_name(r, lang)

        # SEARCH_HISTORY_SQL can match more than one ticker for a loose
        # partial query — group by ticker (preserving first-seen order,
        # already ticker-then-year-DESC per the query's own ORDER BY) so
        # each distinct matched company gets its own deep-dive block.
        rows_by_ticker: dict[str, list[dict]] = {}
        for r in search_results:
            rows_by_ticker.setdefault(r["ticker"], []).append(r)
        deep_dives = [build_deep_dive(ticker, rows) for ticker, rows in rows_by_ticker.items()]

    return render_template_string(
        DASHBOARD_TEMPLATE,
        top10=top10, worst10=worst10, q=q, q_suffix=q_suffix,
        search_results=search_results, deep_dives=deep_dives, lang=lang, t=UI_STRINGS[lang],
    )
