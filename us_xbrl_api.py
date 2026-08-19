
"""
ingestion/experimental/phase48/deployment_package/us_xbrl_api.py
 
INTEGRATION-READY. Deployed and verified live in production (Phase 51).
 
Namespace: /us-xbrl/*  (fully isolated from existing Saudi routes: /, /health)
Uses SQL-over-HTTP via `requests` (NEON_CONNECTION_STRING -> NEON_SQL_URL),
same pattern as app.py. app_readonly role granted SELECT on us_xbrl schema.
"""
from flask import Blueprint, jsonify, render_template_string
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
 
 
DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="en" dir="ltr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>US XBRL Pilot — SEC EDGAR Data</title>
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
</style>
</head>
<body>
  <h1>US XBRL Pilot <span class="badge">SEC EDGAR — Production Pilot</span></h1>
  <div class="subtitle">Live query from Neon (us_xbrl schema) — no caching, isolated from Saudi dashboard data.</div>
 
  <table>
    <tr><th>Ticker</th><th>Company</th><th>Metric</th><th>Value</th><th>Fiscal Year</th><th>Concept</th><th>Confidence</th><th>Status</th><th>Filing</th></tr>
    {% for r in rows %}
    <tr>
      <td><b>{{ r.ticker }}</b></td>
      <td>{{ r.company }}</td>
      <td>{{ r.metric }}</td>
      {% if r.value is not none %}
      <td class="{{ 'neg' if r.value|float < 0 else 'pos' }}">${{ "{:,.0f}".format(r.value|float) }}</td>
      {% else %}
      <td class="na">—</td>
      {% endif %}
      <td>{{ r.fiscal_year or '—' }}</td>
      <td>{{ r.concept or '—' }}</td>
      <td class="conf-{{ r.confidence }}">{{ r.confidence }}</td>
      <td class="status-{{ r.status }}">{{ r.status }}</td>
      <td>{% if r.filing %}<span class="source-link">{{ r.filing }}</span>{% else %}—{% endif %}</td>
    </tr>
    {% endfor %}
  </table>
 
  <div class="footer">
    Every value traces to a real SEC EDGAR 10-K filing. STRUCTURALLY_UNAVAILABLE means no matching XBRL concept exists for that company — shown honestly as empty, never defaulted to zero.
    This page is read-only and isolated from the Saudi petrochemical dashboard.
  </div>
</body>
</html>
"""
 
 
@us_xbrl_bp.route("/")
def us_xbrl_dashboard():
    rows = run_query("""SELECT ticker, company, metric, value, unit, fiscal_year,
                                concept, confidence, status, filing
                         FROM us_xbrl.canonical_financial_records ORDER BY ticker;""")
    return render_template_string(DASHBOARD_TEMPLATE, rows=rows)
