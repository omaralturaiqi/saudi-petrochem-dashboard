
"""
ingestion/experimental/phase48/deployment_package/us_xbrl_api.py
 
INTEGRATION-READY. Deployed and verified live in production (Phase 51).
 
Namespace: /us-xbrl/*  (fully isolated from existing Saudi routes: /, /health)
Uses SQL-over-HTTP via `requests` (NEON_CONNECTION_STRING -> NEON_SQL_URL),
same pattern as app.py. app_readonly role granted SELECT on us_xbrl schema.
"""
from flask import Blueprint, jsonify, render_template_string, request
import os, requests
 
us_xbrl_bp = Blueprint("us_xbrl", __name__, url_prefix="/us-xbrl")
 
NEON_CONNECTION_STRING = os.environ.get("NEON_CONNECTION_STRING", "<PRODUCTION_SECRET>")
NEON_HOST = NEON_CONNECTION_STRING.split("@")[1].split("/")[0] if "@" in NEON_CONNECTION_STRING else None
NEON_SQL_URL = f"https://{NEON_HOST}/sql" if NEON_HOST else None
 
 
def run_query(sql, params=None):
    """Phase 53: params is a list bound to $1, $2, ... placeholders in sql,
    passed through to Neon's SQL-over-HTTP endpoint as real parameterized
    query bindings (verified server-side, not client-side string escaping).
    Same optional-params pattern as app.py's run_query."""
    body = {"query": sql}
    if params is not None:
        body["params"] = params
    resp = requests.post(NEON_SQL_URL, headers={"Neon-Connection-String": NEON_CONNECTION_STRING,
                                                   "Content-Type": "application/json"},
                          json=body, timeout=15)
    resp.raise_for_status()
    return resp.json().get("rows", [])
 
 
@us_xbrl_bp.route("/health")
def us_xbrl_health():
    return jsonify({"status": "ok", "schema": "us_xbrl"})
 
 
@us_xbrl_bp.route("/company/<ticker>")
def get_company(ticker: str):
    ticker = ticker.upper().replace("'", "")  # unchanged: preserves prior observable behavior exactly
    rows = run_query("""SELECT company, ticker, cik, metric, value, unit, period_start, period_end,
                                fiscal_year, concept, source, filing, form, confidence, status
                         FROM us_xbrl.canonical_financial_records WHERE ticker = $1;""", [ticker])
    if not rows:
        return jsonify({"ticker": ticker, "metrics": [], "status": "NOT_FOUND"}), 404
    return jsonify({"ticker": ticker, "metrics": rows, "source": "SEC EDGAR via us_xbrl (production)"})
 
 

# Phase 59: presentation-only translation dictionary. Never mutates r.status,
# r.confidence, r.ticker, r.metric, r.concept, r.filing, or r.value - those
# stay exactly as returned by run_query(). This dict is looked up purely for
# display labels inside the Jinja2 template below.
UI_STRINGS = {
    "en": {
        "html_lang": "en", "html_dir": "ltr",
        "page_title": "US XBRL Pilot — SEC EDGAR Data",
        "heading": "US XBRL Pilot",
        "badge": "SEC EDGAR — Production Pilot",
        "subtitle": "Live query from Neon (us_xbrl schema) — no caching, isolated from Saudi dashboard data.",
        "lang_switch_ar": "العربية", "lang_switch_en": "English",
        "th_ticker": "Ticker", "th_company": "Company", "th_metric": "Metric",
        "th_value": "Value", "th_fiscal_year": "Fiscal Year", "th_concept": "Concept",
        "th_confidence": "Confidence", "th_status": "Status", "th_filing": "Filing",
        "status_labels": {
            "DIRECT": "Direct", "ALTERNATIVE": "Alternative Source",
            "STRUCTURALLY_UNAVAILABLE": "Structurally Unavailable", "NOT_VERIFIED": "Not Verified",
        },
        "confidence_labels": {
            "HIGH": "High", "MEDIUM": "Medium", "LOW": "Low", "UNRESOLVED": "Unresolved",
        },
        "footer_1": "Every value traces to a real SEC EDGAR 10-K filing. Structurally Unavailable means no matching XBRL concept exists for that company — shown honestly as empty, never defaulted to zero.",
        "footer_2": "This page is read-only and isolated from the Saudi petrochemical dashboard.",
    },
    "ar": {
        "html_lang": "ar", "html_dir": "rtl",
        "page_title": "لوحة بيانات US XBRL",
        "heading": "لوحة بيانات US XBRL",
        "badge": "SEC EDGAR — بيئة إنتاج تجريبية",
        "subtitle": "استعلام مباشر من Neon (مخطط us_xbrl) — بدون تخزين مؤقت ومعزول عن بيانات لوحة السعودية.",
        "lang_switch_ar": "العربية", "lang_switch_en": "English",
        "th_ticker": "الرمز", "th_company": "الشركة", "th_metric": "المؤشر",
        "th_value": "القيمة", "th_fiscal_year": "السنة المالية", "th_concept": "المفهوم",
        "th_confidence": "درجة الثقة", "th_status": "الحالة", "th_filing": "الإيداع",
        "status_labels": {
            "DIRECT": "مباشر", "ALTERNATIVE": "مصدر بديل",
            "STRUCTURALLY_UNAVAILABLE": "غير متاح هيكليًا", "NOT_VERIFIED": "غير موثَّق",
        },
        "confidence_labels": {
            "HIGH": "عالية", "MEDIUM": "متوسطة", "LOW": "منخفضة", "UNRESOLVED": "غير محسومة",
        },
        "footer_1": "كل رقم أعلاه يعود لملف تقديم SEC EDGAR حقيقي (10-K). \"غير متاح هيكليًا\" تعني عدم وجود مفهوم XBRL مطابق لهذه الشركة — تُعرَض بأمانة كخانة فارغة، ولا تتحول أبدًا إلى صفر.",
        "footer_2": "هذه الصفحة للقراءة فقط ومعزولة تمامًا عن لوحة البتروكيماويات السعودية.",
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
  .subtitle { color:#8a8f98; font-size:13px; margin-bottom:24px; }
  .badge { display:inline-block; background:#1c2333; color:#7dd3fc; border-radius:4px; padding:2px 8px; font-size:11px; margin-right:6px; }
  table { width:100%; border-collapse: collapse; margin-bottom:32px; background:#12151c; border-radius:8px; overflow:hidden; }
  th, td { padding:10px 14px; text-align:left; border-bottom:1px solid #1e222c; font-size:13px; }
  th { background:#171b24; color:#9aa4b2; font-weight:600; font-size:12px; }
  tr:hover td { background:#161a23; }
  .neg { color:#f87171; }
  .pos { color:#4ade80; }
  .status-DIRECT { color:#4ade80; }
  .status-ALTERNATIVE { color:#fbbf24; }
  .status-STRUCTURALLY_UNAVAILABLE, .status-NOT_VERIFIED { color:#f87171; font-style:italic; }
  .conf-HIGH { color:#4ade80; }
  .conf-MEDIUM { color:#fbbf24; }
  .conf-LOW, .conf-UNRESOLVED { color:#f87171; }
  .source-link { color:#7dd3fc; text-decoration:none; font-size:12px; }
  .footer { color:#5b6472; font-size:12px; margin-top:40px; border-top:1px solid #1e222c; padding-top:16px; }
  .na { color:#5b6472; font-style:italic; }
  .lang-switch { float:right; font-size:13px; margin-bottom:16px; }
  .lang-switch a { color:#8a8f98; text-decoration:none; margin-left:8px; }
  .lang-switch a.active { color:#7dd3fc; font-weight:600; }
  [dir="rtl"] .lang-switch { float:left; }
  [dir="rtl"] .lang-switch a { margin-left:0; margin-right:8px; }
  [dir="rtl"] .ticker,
  [dir="rtl"] .numeric,
  [dir="rtl"] .filing-id,
  [dir="rtl"] .fiscal-year,
  [dir="rtl"] .concept-id {
    direction: ltr;
    unicode-bidi: isolate;
    display: inline-block;
  }
</style>
</head>
<body>
  <div class="lang-switch">
    <a href="?lang=ar" class="{{ 'active' if lang=='ar' else '' }}">{{ t.lang_switch_ar }}</a>
    <a href="?lang=en" class="{{ 'active' if lang=='en' else '' }}">{{ t.lang_switch_en }}</a>
  </div>
  <h1>{{ t.heading }} <span class="badge">{{ t.badge }}</span></h1>
  <div class="subtitle">{{ t.subtitle }}</div>

  <table>
    <tr><th>{{ t.th_ticker }}</th><th>{{ t.th_company }}</th><th>{{ t.th_metric }}</th><th>{{ t.th_value }}</th><th>{{ t.th_fiscal_year }}</th><th>{{ t.th_concept }}</th><th>{{ t.th_confidence }}</th><th>{{ t.th_status }}</th><th>{{ t.th_filing }}</th></tr>
    {% for r in rows %}
    <tr>
      <td><b class="ticker">{{ r.ticker }}</b></td>
      <td>{{ r.company }}</td>
      <td class="concept-id">{{ r.metric }}</td>
      {% if r.value is not none %}
      <td class="numeric {{ 'neg' if r.value|float < 0 else 'pos' }}">${{ "{:,.0f}".format(r.value|float) }}</td>
      {% else %}
      <td class="na">—</td>
      {% endif %}
      <td class="fiscal-year">{{ r.fiscal_year or '—' }}</td>
      <td class="concept-id">{{ r.concept or '—' }}</td>
      <td class="conf-{{ r.confidence }}">{{ t.confidence_labels.get(r.confidence, r.confidence) }}</td>
      <td class="status-{{ r.status }}">{{ t.status_labels.get(r.status, r.status) }}</td>
      <td>{% if r.filing %}<span class="source-link filing-id">{{ r.filing }}</span>{% else %}—{% endif %}</td>
    </tr>
    {% endfor %}
  </table>

  <div class="footer">
    {{ t.footer_1 }}
    {{ t.footer_2 }}
  </div>
</body>
</html>
"""
 
 
@us_xbrl_bp.route("/")
def us_xbrl_dashboard():
    # Phase 59: lang is validated against an explicit allow-list and never
    # touches SQL, run_query(), or any API endpoint. Any value outside
    # {"ar","en"} - missing, invalid, or malicious - safely falls back to "en".
    lang = request.args.get("lang", "en")
    if lang not in ("ar", "en"):
        lang = "en"

    rows = run_query("""SELECT ticker, company, metric, value, unit, fiscal_year,
                                concept, confidence, status, filing
                         FROM us_xbrl.canonical_financial_records ORDER BY ticker;""")
    return render_template_string(DASHBOARD_TEMPLATE, rows=rows, lang=lang, t=UI_STRINGS[lang])
