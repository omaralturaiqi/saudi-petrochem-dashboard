#!/usr/bin/env python3
"""
scripts/ingest_financial_line_item.py

A single-record CLI for building one core.financial_line_items INSERT
statement from explicit, manually-provided values. This is deliberately
NOT a PDF-to-database pipeline — it exists to close the gap documented in
README.md and ingestion/load_historical.py (extract_facts()/
register_source_document()/load_facts() remain unimplemented TODOs; the
62 rows currently live were entered via an ad-hoc, unpreserved script)
with the smallest possible real, repeatable, reviewable write PATH —
one record at a time, on purpose, to keep the blast radius of any mistake
to a single row.

**This script has no Neon write path.** It builds a single INSERT
statement and prints it to stdout — nothing else. Like every other
database-write script in this project's history so far
(scripts/fetch_market_prices.py, scripts/fetch_market_index.py), the
actual execution against Neon is a separate, manual, human-reviewed step.

WHAT THIS SCRIPT DOES:
  1. Resolves --ticker to a real company_id via a READ-ONLY SELECT against
     core.companies — company_id is never hardcoded or guessed.
  2. Verifies --source-document-id actually exists in
     core.source_documents via a READ-ONLY SELECT — if it does not, this
     script refuses to build the INSERT (a clear error, not a row with a
     broken foreign key).
  3. Requires --value to be an explicit, real, finite number. There is
     NO --estimate flag and none will be added to this script — it exists
     to record a value someone actually extracted, not to guess one.
  4. Builds exactly ONE parameterized-value INSERT statement (all 25
     columns of core.financial_line_items, including line_item_id via
     gen_random_uuid() and extracted_at via now() — the same SQL-level
     defaults the table itself uses, made explicit here rather than
     omitted, so every column is visibly accounted for) and prints it to
     stdout. Nothing is written to any file, nothing is sent to Neon.

DEFAULTS — deliberately chosen, one of them differs from what was
initially requested for this script, and why:
  - is_restated defaults to NULL (unknown/not checked), NOT to FALSE.
    schema.sql's own documented Phase 0.5 rule for this exact column says:
    "NULL = unknown/not checked, not FALSE-by-default... never assume
    'not restated' just because we didn't check." Defaulting this CLI to
    FALSE would silently violate that rule for every row it produces
    unless the operator remembered to override it every time. --is-restated
    {true,false} is available to set it explicitly when actually known;
    omitting the flag leaves it NULL, matching the schema's own rule.
  - reviewed_by_human defaults to FALSE, matching core.financial_line_items'
    own real column default (schema.sql: `BOOLEAN NOT NULL DEFAULT FALSE`)
    — no conflict here, --reviewed-by-human is a flag to set it TRUE.

USAGE (see also: python3 scripts/ingest_financial_line_item.py --help):
    python3 scripts/ingest_financial_line_item.py \\
        --ticker 2010 --fiscal-year 2025 \\
        --concept net_income_attributable_to_parent \\
        --value 1234567 --statement-type income_statement \\
        --period-type FY --period-end 2025-12-31 \\
        --unit thousand --currency SAR \\
        --source-document-id <uuid> --extraction-method manual \\
        --confidence MEDIUM --reported-label "..." --source-page 172

    requires NEON_CONNECTION_STRING in the environment for the two
    read-only lookups (company_id, source_document_id existence) — same
    variable app.py uses. No write capability is implied by having it set;
    this script never issues INSERT/UPDATE/DELETE/DDL against Neon itself.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import date, datetime

STATEMENT_TYPES = ("income_statement", "balance_sheet", "cash_flow", "equity_changes", "segment", "other")
PERIOD_TYPES = ("FY", "Q1", "Q2", "Q3", "Q4", "H1", "H2")
UNITS = ("unit", "thousand", "million")
CONFIDENCE_LEVELS = ("HIGH", "MEDIUM", "LOW")


def get_neon_sql_url() -> str:
    conn = os.environ.get("NEON_CONNECTION_STRING")
    if not conn:
        raise RuntimeError(
            "NEON_CONNECTION_STRING environment variable is not set. Required "
            "for the two read-only lookups this script performs (company_id, "
            "source_document_id existence) — same variable app.py uses. No "
            "write capability is implied; this script never issues INSERT/"
            "UPDATE/DELETE/DDL against Neon itself."
        )
    host = conn.split("@")[1].split("/")[0]
    return f"https://{host}/sql"


def run_query(sql: str, params: list | None = None) -> list[dict]:
    """Read-only helper — every call site in this script issues SELECT
    only, never INSERT/UPDATE/DELETE/DDL."""
    import requests

    neon_sql_url = get_neon_sql_url()
    body = {"query": sql}
    if params is not None:
        body["params"] = params
    resp = requests.post(
        neon_sql_url,
        headers={
            "Neon-Connection-String": os.environ["NEON_CONNECTION_STRING"],
            "Content-Type": "application/json",
        },
        json=body,
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"query failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()["rows"]


def resolve_company_id(ticker: str) -> str:
    rows = run_query("SELECT company_id FROM core.companies WHERE ticker = $1;", [ticker])
    if not rows:
        raise ValueError(f"no company found in core.companies with ticker={ticker!r} — refusing to guess a company_id")
    return rows[0]["company_id"]


def verify_source_document_exists(document_id: str) -> None:
    rows = run_query("SELECT 1 FROM core.source_documents WHERE document_id = $1;", [document_id])
    if not rows:
        raise ValueError(
            f"document_id={document_id!r} does not exist in core.source_documents — "
            "refusing to build an INSERT with a broken foreign key. Verify the UUID, "
            "or register the source document first."
        )


def parse_value(raw: str) -> float:
    """Requires an explicit, real, finite number. Empty strings and
    non-numeric input are rejected by argparse's type=float itself before
    this ever runs; this adds the NaN/inf guard argparse's float() alone
    would not catch, and exists precisely so there is no path to an
    'estimate' silently becoming value_raw."""
    value = float(raw)
    if math.isnan(value) or math.isinf(value):
        raise ValueError(f"--value must be a real, finite number — got {raw!r}")
    return value


def parse_date(raw: str) -> date:
    return datetime.strptime(raw, "%Y-%m-%d").date()


def _sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, date):
        return f"'{value.isoformat()}'"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(value)


def build_insert_sql(fields: dict) -> str:
    """Builds exactly one INSERT statement for core.financial_line_items,
    all 25 columns explicit and in schema.sql's column order — including
    line_item_id (gen_random_uuid()) and extracted_at (now()) as literal
    SQL-side defaults, matching the table's own defaults exactly rather
    than omitting them. Every string value goes through _sql_literal(),
    which doubles embedded single quotes — the same escaping discipline
    used by scripts/fetch_market_prices.py and scripts/fetch_market_index.py
    — so no CLI input, however it was sourced, can break out of its
    literal and inject additional SQL."""
    columns = (
        "line_item_id, company_id, document_id, statement_type, concept, "
        "reported_label, fiscal_year, fiscal_quarter, period_start, period_end, "
        "value_raw, currency, unit, is_restated, restated_from_line_item_id, "
        "source_page, extraction_method, confidence, raw_text, extracted_at, "
        "parser_version, reviewed_by_human, review_notes, period_type, fiscal_half"
    )
    values = ", ".join([
        "gen_random_uuid()",
        _sql_literal(fields["company_id"]),
        _sql_literal(fields["document_id"]),
        _sql_literal(fields["statement_type"]),
        _sql_literal(fields["concept"]),
        _sql_literal(fields["reported_label"]),
        _sql_literal(fields["fiscal_year"]),
        _sql_literal(fields["fiscal_quarter"]),
        _sql_literal(fields["period_start"]),
        _sql_literal(fields["period_end"]),
        _sql_literal(fields["value_raw"]),
        _sql_literal(fields["currency"]),
        _sql_literal(fields["unit"]),
        _sql_literal(fields["is_restated"]),
        _sql_literal(fields["restated_from_line_item_id"]),
        _sql_literal(fields["source_page"]),
        _sql_literal(fields["extraction_method"]),
        _sql_literal(fields["confidence"]),
        _sql_literal(fields["raw_text"]),
        "now()",
        _sql_literal(fields["parser_version"]),
        _sql_literal(fields["reviewed_by_human"]),
        _sql_literal(fields["review_notes"]),
        _sql_literal(fields["period_type"]),
        _sql_literal(fields["fiscal_half"]),
    ])
    return f"INSERT INTO core.financial_line_items ({columns})\nVALUES ({values});"


def build_arg_parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    cli.add_argument("--ticker", required=True, help="resolved to company_id via core.companies — never hardcoded")
    cli.add_argument("--fiscal-year", required=True, type=int)
    cli.add_argument("--fiscal-quarter", type=int, choices=[1, 2, 3, 4], default=None)
    cli.add_argument("--fiscal-half", type=int, choices=[1, 2], default=None)
    cli.add_argument("--concept", required=True)
    cli.add_argument("--value", required=True, type=parse_value,
                      help="a real, finite number — required, no default, no --estimate flag exists")
    cli.add_argument("--statement-type", required=True, choices=STATEMENT_TYPES)
    cli.add_argument("--period-type", required=True, choices=PERIOD_TYPES)
    cli.add_argument("--period-start", type=parse_date, default=None)
    cli.add_argument("--period-end", required=True, type=parse_date)
    cli.add_argument("--unit", required=True, choices=UNITS)
    cli.add_argument("--currency", default="SAR")
    cli.add_argument("--source-document-id", required=True, help="verified to exist in core.source_documents before building any SQL")
    cli.add_argument("--extraction-method", required=True)
    cli.add_argument("--confidence", required=True, choices=CONFIDENCE_LEVELS)
    cli.add_argument("--reported-label", required=True)
    cli.add_argument("--source-page", type=int, default=None)
    cli.add_argument("--raw-text", default=None)
    cli.add_argument("--parser-version", default=None)
    cli.add_argument("--is-restated", choices=["true", "false"], default=None,
                      help="omit to leave NULL (unknown/not checked) — this schema's own documented rule; "
                           "never defaults to false")
    cli.add_argument("--restated-from-line-item-id", default=None)
    cli.add_argument("--reviewed-by-human", action="store_true", default=False)
    cli.add_argument("--review-notes", default=None)
    return cli


def main() -> None:
    cli = build_arg_parser()
    args = cli.parse_args()

    try:
        company_id = resolve_company_id(args.ticker)
        verify_source_document_exists(args.source_document_id)
    except Exception as e:
        print(f"REFUSED: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

    fields = {
        "company_id": company_id,
        "document_id": args.source_document_id,
        "statement_type": args.statement_type,
        "concept": args.concept,
        "reported_label": args.reported_label,
        "fiscal_year": args.fiscal_year,
        "fiscal_quarter": args.fiscal_quarter,
        "period_start": args.period_start,
        "period_end": args.period_end,
        "value_raw": args.value,
        "currency": args.currency,
        "unit": args.unit,
        "is_restated": {"true": True, "false": False, None: None}[args.is_restated],
        "restated_from_line_item_id": args.restated_from_line_item_id,
        "source_page": args.source_page,
        "extraction_method": args.extraction_method,
        "confidence": args.confidence,
        "raw_text": args.raw_text,
        "parser_version": args.parser_version,
        "reviewed_by_human": args.reviewed_by_human,
        "review_notes": args.review_notes,
        "period_type": args.period_type,
        "fiscal_half": args.fiscal_half,
    }

    sql = build_insert_sql(fields)
    print("=" * 78)
    print("INGEST FINANCIAL LINE ITEM — single-record INSERT, NOT executed against Neon")
    print(f"ticker={args.ticker!r} resolved company_id={company_id}")
    print(f"source_document_id verified to exist: {args.source_document_id}")
    print("=" * 78)
    print(sql)
    print("=" * 78)
    print("NEON WRITES ISSUED BY THIS SCRIPT: 0 — review the statement above, then run it manually.")


if __name__ == "__main__":
    main()
