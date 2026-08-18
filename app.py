"""
Saudi Petrochemical Intelligence — read-only browsing app.

Purpose: let the person actually SEE what's in the Neon database in a browser,
without needing the Neon dashboard or SQL knowledge. This is a viewer, not a
data-entry tool — every write to this data happens through the ingestion
pipeline (parser.py etc.), never through this web app.

Connects to Neon over its HTTP SQL endpoint (same one used to build the
schema this session) rather than a raw TCP connection, since that's what
worked from this environment — and it's also genuinely the right choice for
a small Render free-tier web service that may cold-start/sleep, since HTTP
mode has no persistent connection pool to re-establish on wake.
"""
import os
import requests
from flask import Flask, render_template_string, abort

app = Flask(__name__)

NEON_CONNECTION_STRING = os.environ.get("NEON_CONNECTION_STRING")
if not NEON_CONNECTION_STRING:
    raise RuntimeError(
        "NEON_CONNECTION_STRING environment variable is not set. "
        "This must be configured in Render's environment variables, never "
        "hardcoded in this file or committed to the repo."
    )
NEON_HOST = NEON_CONNECTION_STRING.split("@")[1].split("/")[0]
NEON_SQL_URL = f"https://{NEON_HOST}/sql"


def run_query(sql, params=None):
    """Read-only helper. This app never issues INSERT/UPDATE/DELETE/DDL —
    enforced by convention here, and should additionally be enforced at the
    database role level (a read-only Postgres role) before any real
    production use, not just by this app's code discipline."""
    body = {"query": sql}
    if params is not None:
        body["params"] = params
    resp = requests.post(
        NEON_SQL_URL,
        headers={
            "Neon-Connection-String": NEON_CONNECTION_STRING,
            "Content-Type": "application/json",
        },
        json=body,
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Query failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


PAGE_TEMPLATE = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Saudi Petrochemical Intelligence — Data Viewer</title>
<style>
  body { font-family: -apple-system, "Segoe UI", Tahoma, sans-serif; background:#0b0d12; color:#e8e8ea; margin:0; padding:24px; }
  h1 { font-size: 20px; color:#fff; margin-bottom:4px; }
  .subtitle { color:#8a8f98; font-size:13px; margin-bottom:24px; }
  .badge { display:inline-block; background:#1c2333; color:#7dd3fc; border-radius:4px; padding:2px 8px; font-size:11px; margin-left:6px; }
  table { width:100%; border-collapse: collapse; margin-bottom:32px; background:#12151c; border-radius:8px; overflow:hidden; }
  th, td { padding:10px 14px; text-align:right; border-bottom:1px solid #1e222c; font-size:13px; }
  th { background:#171b24; color:#9aa4b2; font-weight:600; text-transform:uppercase; font-size:11px; letter-spacing:0.03em; }
  tr:hover td { background:#161a23; }
  .neg { color:#f87171; }
  .pos { color:#4ade80; }
  .conf-HIGH { color:#4ade80; }
  .conf-MEDIUM { color:#fbbf24; }
  .conf-LOW { color:#f87171; }
  .empty { color:#5b6472; font-style:italic; padding:20px; text-align:center; }
  .source-link { color:#7dd3fc; text-decoration:none; font-size:12px; }
  .footer { color:#5b6472; font-size:12px; margin-top:40px; border-top:1px solid #1e222c; padding-top:16px; }
  section { margin-bottom:40px; }
  h2 { font-size:15px; color:#c8ccd4; margin-bottom:10px; }
</style>
</head>
<body>
  <h1>Saudi Petrochemical Intelligence <span class="badge">read-only viewer</span></h1>
  <div class="subtitle">Data straight from the Neon database — no caching, every load is a live query.</div>

  <section>
    <h2>Companies ({{ companies|length }})</h2>
    {% if companies %}
    <table>
      <tr><th>Ticker</th><th>Name (EN)</th><th>Name (AR)</th><th>Sector</th><th>Status</th></tr>
      {% for c in companies %}
      <tr><td>{{ c.ticker }}</td><td>{{ c.name_en }}</td><td>{{ c.name_ar }}</td><td>{{ c.sector or '—' }}</td><td>{{ c.status }}</td></tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="empty">No companies loaded yet.</div>
    {% endif %}
  </section>

  <section>
    <h2>Financial Line Items ({{ financials|length }})</h2>
    {% if financials %}
    <table>
      <tr><th>Ticker</th><th>Concept</th><th>FY</th><th>Value</th><th>Unit</th><th>Confidence</th><th>Source Page</th><th>Source</th></tr>
      {% for f in financials %}
      <tr>
        <td>{{ f.ticker }}</td>
        <td>{{ f.concept }}</td>
        <td>{{ f.fiscal_year }}</td>
        <td class="{{ 'neg' if f.value_raw|float < 0 else 'pos' }}">{{ "{:,.2f}".format(f.value_raw|float) }}</td>
        <td>{{ f.unit }}</td>
        <td class="conf-{{ f.confidence }}">{{ f.confidence }}</td>
        <td>{{ f.source_page or '—' }}</td>
        <td><a class="source-link" href="{{ f.source_url }}" target="_blank">official PDF ↗</a></td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="empty">No financial data loaded yet.</div>
    {% endif %}
  </section>

  <section>
    <h2>Source Documents ({{ documents|length }})</h2>
    {% if documents %}
    <table>
      <tr><th>Ticker</th><th>Type</th><th>FY</th><th>Website</th><th>SHA256 (first 16)</th><th>Pages</th></tr>
      {% for d in documents %}
      <tr>
        <td>{{ d.ticker }}</td>
        <td>{{ d.document_type }}</td>
        <td>{{ d.fiscal_year }}</td>
        <td>{{ d.source_website }}</td>
        <td><code>{{ d.document_sha256[:16] }}…</code></td>
        <td>{{ d.page_count or '—' }}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="empty">No source documents loaded yet.</div>
    {% endif %}
  </section>

  <div class="footer">
    Data provenance: every number above traces back to an official company filing (SHA256-hashed PDF).
    This viewer performs no writes — it only reads. Free-tier hosting: this page may take 30–60s to load
    on first visit after inactivity while the service wakes up.
  </div>
</body>
</html>
"""


@app.route("/")
def index():
    try:
        companies = run_query(
            "SELECT ticker, name_en, name_ar, sector, status FROM core.companies ORDER BY ticker;"
        )["rows"]
        financials = run_query("""
            SELECT c.ticker, f.concept, f.fiscal_year, f.value_raw, f.unit,
                   f.confidence, f.source_page, s.source_url
            FROM core.financial_line_items f
            JOIN core.companies c ON c.company_id = f.company_id
            JOIN core.source_documents s ON s.document_id = f.document_id
            ORDER BY c.ticker, f.concept, f.fiscal_year;
        """)["rows"]
        documents = run_query("""
            SELECT c.ticker, s.document_type, s.fiscal_year, s.source_website,
                   s.document_sha256, s.page_count
            FROM core.source_documents s
            JOIN core.companies c ON c.company_id = s.company_id
            ORDER BY c.ticker;
        """)["rows"]
    except Exception as e:
        abort(500, description=str(e))

    return render_template_string(
        PAGE_TEMPLATE, companies=companies, financials=financials, documents=documents
    )


@app.route("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
