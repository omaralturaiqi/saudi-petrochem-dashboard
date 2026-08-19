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
import logging
import requests
from flask import Flask, render_template_string, abort
from us_xbrl_api import us_xbrl_bp
app = Flask(__name__)
app.register_blueprint(us_xbrl_bp)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("saudi-petrochem-dashboard")

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


CONCEPT_LABELS_AR = {
    "revenue": "الإيرادات",
    "net_income": "صافي الدخل",
    "eps_basic": "ربحية السهم",
    "gross_profit": "إجمالي الربح",
    "operating_income": "الدخل التشغيلي",
    "total_assets": "إجمالي الأصول",
    "total_equity": "إجمالي حقوق الملكية",
    "total_liabilities": "إجمالي الالتزامات",
    "cash_and_equivalents": "النقد وما في حكمه",
    "cfo": "التدفق النقدي التشغيلي",
    "capex": "المصروفات الرأسمالية",
}
STATUS_LABELS_AR = {
    "active": "نشطة",
    "delisted": "مُشطّبة",
    "merged": "مندمجة",
    "renamed": "معاد تسميتها",
    "acquired": "مستحوذ عليها",
    "suspended": "موقوفة",
}
DOCTYPE_LABELS_AR = {
    "annual_report": "تقرير سنوي",
    "quarterly_report": "تقرير ربعي",
    "disclosure_announcement": "إفصاح",
    "earnings_presentation": "عرض نتائج",
    "prospectus": "نشرة إصدار",
    "other": "أخرى",
}

PAGE_TEMPLATE = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>منصة تحليل البتروكيماويات السعودية</title>
<style>
  body { font-family: "Segoe UI", Tahoma, "Noto Sans Arabic", sans-serif; background:#0b0d12; color:#e8e8ea; margin:0; padding:24px; }
  h1 { font-size: 20px; color:#fff; margin-bottom:4px; }
  .subtitle { color:#8a8f98; font-size:13px; margin-bottom:24px; }
  .badge { display:inline-block; background:#1c2333; color:#7dd3fc; border-radius:4px; padding:2px 8px; font-size:11px; margin-right:6px; }
  table { width:100%; border-collapse: collapse; margin-bottom:32px; background:#12151c; border-radius:8px; overflow:hidden; }
  th, td { padding:10px 14px; text-align:right; border-bottom:1px solid #1e222c; font-size:13px; }
  th { background:#171b24; color:#9aa4b2; font-weight:600; font-size:12px; }
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
  .en { direction:ltr; unicode-bidi:embed; }
</style>
</head>
<body>
  <h1>منصة تحليل البتروكيماويات السعودية <span class="badge">للعرض فقط</span></h1>
  <div class="subtitle">البيانات مباشرة من قاعدة Neon — بدون تخزين مؤقت، كل تحميل استعلام حي.</div>

  <section>
    <h2>الشركات ({{ companies|length }})</h2>
    {% if companies %}
    <table>
      <tr><th>الرمز</th><th>الاسم بالإنجليزية</th><th>الاسم بالعربية</th><th>القطاع</th><th>الحالة</th></tr>
      {% for c in companies %}
      <tr>
        <td class="en">{{ c.ticker }}</td>
        <td class="en">{{ c.name_en }}</td>
        <td>{{ c.name_ar }}</td>
        <td>{{ c.sector or '—' }}</td>
        <td>{{ status_labels.get(c.status, c.status) }}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="empty">لا توجد شركات مُحمّلة بعد.</div>
    {% endif %}
  </section>

  <section>
    <h2>البنود المالية ({{ financials|length }})</h2>
    {% if financials %}
    <table>
      <tr><th>الرمز</th><th>البند</th><th>السنة المالية</th><th>القيمة</th><th>الوحدة</th><th>مستوى الثقة</th><th>رقم الصفحة</th><th>المصدر</th></tr>
      {% for f in financials %}
      <tr>
        <td class="en">{{ f.ticker }}</td>
        <td>{{ concept_labels.get(f.concept, f.concept) }}</td>
        <td class="en">{{ f.fiscal_year }}</td>
        <td class="en {{ 'neg' if f.value_raw|float < 0 else 'pos' }}">{{ "{:,.2f}".format(f.value_raw|float) }}</td>
        <td>{{ 'ألف' if f.unit == 'thousand' else ('مليون' if f.unit == 'million' else 'وحدة') }}</td>
        <td class="conf-{{ f.confidence }}">{{ {'HIGH':'عالي','MEDIUM':'متوسط','LOW':'منخفض'}.get(f.confidence, f.confidence) }}</td>
        <td class="en">{{ f.source_page or '—' }}</td>
        <td><a class="source-link" href="{{ f.source_url }}" target="_blank">المصدر الرسمي ↗</a></td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="empty">لا توجد بيانات مالية مُحمّلة بعد.</div>
    {% endif %}
  </section>

  <section>
    <h2>المستندات المصدرية ({{ documents|length }})</h2>
    {% if documents %}
    <table>
      <tr><th>الرمز</th><th>نوع المستند</th><th>السنة المالية</th><th>الموقع</th><th>بصمة SHA256</th><th>عدد الصفحات</th></tr>
      {% for d in documents %}
      <tr>
        <td class="en">{{ d.ticker }}</td>
        <td>{{ doctype_labels.get(d.document_type, d.document_type) }}</td>
        <td class="en">{{ d.fiscal_year }}</td>
        <td class="en">{{ d.source_website }}</td>
        <td class="en"><code>{{ d.document_sha256[:16] }}…</code></td>
        <td class="en">{{ d.page_count or '—' }}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="empty">لا توجد مستندات مصدرية مُحمّلة بعد.</div>
    {% endif %}
  </section>

  <div class="footer">
    تتبّع المصدر: كل رقم أعلاه يعود لمصدر رسمي (ملف PDF موثّق ببصمة SHA256). هذه الصفحة للقراءة فقط ولا تقوم بأي تعديل على البيانات.
    استضافة الطبقة المجانية: قد تستغرق الصفحة ٣٠-٦٠ ثانية عند أول زيارة بعد فترة خمول ريثما تستيقظ الخدمة.
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
        # SECURITY: never expose exception text, SQL, connection details, or
        # stack traces to anonymous users. Log server-side only (visible in
        # Render's logs), return a generic message to the browser.
        logger.error("Failed to load dashboard data: %s", e, exc_info=True)
        abort(500, description="Unable to load data right now. Please try again shortly.")

    return render_template_string(
        PAGE_TEMPLATE,
        companies=companies,
        financials=financials,
        documents=documents,
        concept_labels=CONCEPT_LABELS_AR,
        status_labels=STATUS_LABELS_AR,
        doctype_labels=DOCTYPE_LABELS_AR,
    )


@app.route("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
