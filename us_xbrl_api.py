"""
ingestion/experimental/phase48/deployment_package/us_xbrl_api.py

INTEGRATION-READY, NOT DEPLOYED. To integrate later:
  from us_xbrl_api import us_xbrl_bp
  app.register_blueprint(us_xbrl_bp)   # <-- ONE line added to app.py, nothing else touched

Namespace: /us-xbrl/*  (fully isolated from existing Saudi routes: /, /health)

CORRECTED Phase 49: app.py's REAL connection pattern is confirmed to be
SQL-over-HTTP via `requests` (NEON_CONNECTION_STRING -> NEON_SQL_URL),
NOT psycopg2. This module now matches that exact pattern, reusing the
SAME NEON_CONNECTION_STRING env var app.py already requires - zero new
environment variables needed.
"""
from flask import Blueprint, jsonify
import os, requests

us_xbrl_bp = Blueprint("us_xbrl", __name__, url_prefix="/us-xbrl")

NEON_CONNECTION_STRING = os.environ.get("NEON_CONNECTION_STRING", "<PRODUCTION_SECRET>")
NEON_HOST = NEON_CONNECTION_STRING.split("@")[1].split("/")[0] if "@" in NEON_CONNECTION_STRING else None
NEON_SQL_URL = f"https://{NEON_HOST}/sql" if NEON_HOST else None


def run_query(sql):
    resp = requests.post(NEON_SQL_URL, headers={"Neon-Connection-String": NEON_CONNECTION_STRING,
                                                   "Content-Type": "application/json"},
                          json={"query": sql}, timeout=15)
    resp.raise_for_status()
    return resp.json().get("rows", [])


@us_xbrl_bp.route("/health")
def us_xbrl_health():
    return jsonify({"status": "ok", "schema": "us_xbrl"})


@us_xbrl_bp.route("/company/<ticker>")
def get_company(ticker: str):
    ticker = ticker.upper().replace("'", "")  # minimal sanitation, GET-only, no write path exists
    rows = run_query(f"""SELECT company, ticker, cik, metric, value, unit, period_start, period_end,
                                fiscal_year, concept, source, filing, form, confidence, status
                         FROM us_xbrl.canonical_financial_records WHERE ticker = '{ticker}';""")
    if not rows:
        return jsonify({"ticker": ticker, "metrics": [], "status": "NOT_FOUND"}), 404
    return jsonify({"ticker": ticker, "metrics": rows, "source": "SEC EDGAR via us_xbrl (production)"})
