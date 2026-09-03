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

WHAT THIS FILE DOES NOT DO:
  - Never writes to Neon — every query here is a read-only SELECT.
  - Never touches app.py's or us_xbrl_api.py's own routes, templates, or
    run_query() — this blueprint is fully self-contained, same as
    us_xbrl_api.py is relative to app.py.
  - Never fabricates a translated company name — see pick_display_name().
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
      {% if search_results %}
        {{ return_table(search_results) }}
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
    if q:
        like_pattern = f"%{q}%"
        # $1 is reused 3 times in SEARCH_HISTORY_SQL (standard PostgreSQL
        # extended-query-protocol behavior: a placeholder can appear
        # multiple times bound to one value) — one param, not three.
        search_results = run_query(SEARCH_HISTORY_SQL, [like_pattern])
        for r in search_results:
            r["display_name"] = pick_display_name(r, lang)

    return render_template_string(
        DASHBOARD_TEMPLATE,
        top10=top10, worst10=worst10, q=q, q_suffix=q_suffix,
        search_results=search_results, lang=lang, t=UI_STRINGS[lang],
    )
