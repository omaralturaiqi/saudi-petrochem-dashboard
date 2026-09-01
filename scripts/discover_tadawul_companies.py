#!/usr/bin/env python3
"""
scripts/discover_tadawul_companies.py

Discovery-only, metadata-only: builds one SQL file of core.companies
INSERT statements for the Tadawul (TASI) main-market companies not
already in core.companies. No PDFs, no financial data, no archive of
any kind — ticker, name_en, sector only, per this task's own scope.

DATA SOURCE — read this before trusting the output:
  This script does NOT fetch anything live. It was attempted this
  session via WebFetch against saudiexchange.sa's official Issuer
  Directory and Argaam's companies-prices page — both were blocked by
  this sandbox's egress proxy, confirmed with a neutral control-domain
  test (example.com also blocked), so the live page/API structure of
  either source could not be independently inspected here.

  Instead, the company list below (RAW_COMPANIES) is a hardcoded
  transcription of a list the project owner states they fetched live
  from argaam.com/en themselves (outside this sandbox) on 2026-09-01,
  then pasted into this session as raw text. This script did NOT fetch
  it and did NOT independently verify it against a live source — it
  only transcribed the pasted list into Python data and checked its
  internal consistency (no duplicate tickers, no missing fields). If
  Argaam's actual list has since changed (new listing, delisting,
  rename), this file will be stale until it is regenerated from a
  fresh paste or a real, verified live-fetch path is built.

  name_ar is not available in the source list (it was English-only) —
  every row leaves name_ar as NULL rather than inventing a translation.

WHAT THIS SCRIPT DOES:
  1. Filters RAW_COMPANIES to exclude:
       - the 3 tickers already in core.companies (2010, 2290, 2330) —
         EXISTING_TICKERS, so this stays correct even if this constant
         needs updating later, not silently baked into the transcription.
       - any ticker present but flagged ambiguous/uncertain — none are
         in this list, but the mechanism (AMBIGUOUS_TICKERS, empty here)
         exists so a future paste can mark one without deleting it
         silently; those are logged as WARNING and excluded, never
         invented a sector/name for.
  2. Verifies no duplicate ticker exists within RAW_COMPANIES itself
     (a transcription error, not a filtering decision) — raises loudly
     if one is found rather than silently deduping.
  3. Builds one INSERT per company, each guarded by
     WHERE NOT EXISTS (SELECT 1 FROM core.companies WHERE ticker = ...)
     — NOT "ON CONFLICT (ticker) DO NOTHING", because core.companies has
     NO real UNIQUE/exclusion constraint on ticker (verified this
     session: schema.sql defines only a partial, non-unique index,
     `CREATE INDEX idx_companies_ticker ON companies(ticker) WHERE
     status = 'active'` — grepped for "unique" near companies with zero
     matches). ON CONFLICT requires a real unique/exclusion constraint
     to target; using it here would either error at execution time or
     silently rely on a constraint that does not exist. WHERE NOT EXISTS
     works regardless of that gap and is honest about why it was chosen.
  4. Writes all INSERTs to tadawul_companies_insert_statements.sql.
     Never writes to Neon directly — no Neon connection of any kind in
     this script, matching scripts/fetch_market_index.py's pattern
     (this script doesn't even need a read-only lookup, since exclusion
     is done from the EXISTING_TICKERS constant, not a live query —
     see the "STALE RISK" note below for why that's a deliberate
     trade-off, not an oversight).

STALE RISK — read before running against a live core.companies that
  has since grown: EXISTING_TICKERS is a hardcoded snapshot (2010, 2290,
  2330), not a live lookup. If core.companies has already grown beyond
  those 3 rows by the time this script is actually run, this script
  will NOT know that and could re-emit INSERTs for tickers already
  added by some other process. The WHERE NOT EXISTS guard in the
  generated SQL is the real safety net for that case (it re-checks
  against the live table at execution time, in Neon, not in this
  script) — EXISTING_TICKERS only controls what this script prints to
  the console as "already present, skipped by this script", not what
  is actually safe to execute.

WHAT THIS SCRIPT DELIBERATELY DOES NOT DO:
  - Never fetches or downloads any PDF, annual report, or other
    document — ticker/name_en/sector only, per this task's scope.
  - Never invents a company, ticker, sector, or name_ar for anything
    ambiguous — an ambiguous entry is WARNED and excluded, not guessed.
  - Never issues INSERT/UPDATE/DELETE/DDL against Neon — this script
    has no Neon connection of any kind, live or otherwise.
  - Never includes ETFs, REITs, or CEFs — the source paste already
    excluded those three groups explicitly; this script does not
    re-derive that filter from ticker ranges (which would be guessing),
    it simply trusts that the paste already excluded them, and this is
    stated here so that trust is visible, not hidden.
  - Never touches the NOMU parallel market — main market (TASI) only,
    per this task's explicit scope; NOMU is a stated separate later task.

USAGE:
    python3 scripts/discover_tadawul_companies.py
        # no environment variables required, no network access needed —
        # this script never talks to Neon or the internet at all.
"""
from __future__ import annotations

# (ticker, name_en, sector) — transcribed verbatim from the pasted list,
# including the 3 already-live tickers (2010, 2290, 2330), so filtering
# happens explicitly in code (see EXISTING_TICKERS) rather than being
# silently baked into the transcription itself.
RAW_COMPANIES: list[tuple[str, str, str]] = [
    # Energy
    ("2222", "SAUDI ARAMCO", "Energy"),
    ("2030", "SARCO", "Energy"),
    ("2380", "PETRO RABIGH", "Energy"),
    ("4030", "BAHRI", "Energy"),
    ("2381", "ARABIAN DRILLING", "Energy"),
    ("2382", "ADES", "Energy"),
    # Materials
    ("1201", "TAKWEEN", "Materials"),
    ("1202", "MEPCO", "Materials"),
    ("1210", "BCI", "Materials"),
    ("1211", "MAADEN", "Materials"),
    ("1301", "ASLAK", "Materials"),
    ("1304", "ALYAMAMAH STEEL", "Materials"),
    ("1320", "SSP", "Materials"),
    ("2001", "CHEMANOL", "Materials"),
    ("2010", "SABIC", "Materials"),
    ("2020", "SABIC AGRI-NUTRIENTS", "Materials"),
    ("2090", "NGC", "Materials"),
    ("2150", "ZOUJAJ", "Materials"),
    ("2170", "ALUJAIN", "Materials"),
    ("2180", "FIPCO", "Materials"),
    ("2200", "APC", "Materials"),
    ("2210", "NAMA CHEMICALS", "Materials"),
    ("2220", "MAADANIYAH", "Materials"),
    ("2240", "SENAAT", "Materials"),
    ("2250", "SIIG", "Materials"),
    ("2290", "YANSAB", "Materials"),
    ("2300", "SPM", "Materials"),
    ("2310", "SIPCHEM", "Materials"),
    ("2330", "ADVANCED", "Materials"),
    ("2350", "SAUDI KAYAN", "Materials"),
    ("3002", "NAJRAN CEMENT", "Materials"),
    ("3003", "CITY CEMENT", "Materials"),
    ("3004", "NORTHERN CEMENT", "Materials"),
    ("3005", "UACC", "Materials"),
    ("3010", "ACC", "Materials"),
    ("3020", "YC", "Materials"),
    ("3030", "SAUDI CEMENT", "Materials"),
    ("3040", "QACCO", "Materials"),
    ("3050", "SPCC", "Materials"),
    ("3060", "YCC", "Materials"),
    ("3080", "EPCCO", "Materials"),
    ("3090", "TCC", "Materials"),
    ("3091", "JOUF CEMENT", "Materials"),
    ("3092", "RIYADH CEMENT", "Materials"),
    ("2060", "TASNEE", "Materials"),
    ("3008", "ALKATHIRI", "Materials"),
    ("3007", "OASIS", "Materials"),
    ("1321", "EAST PIPES", "Materials"),
    ("1322", "AMAK", "Materials"),
    ("2223", "LUBEREF", "Materials"),
    ("1324", "SALEH ALRASHED", "Materials"),
    ("2360", "SVCP", "Materials"),
    ("1323", "UCIC", "Materials"),
    ("4143", "TALCO", "Materials"),
    # Capital Goods
    ("1212", "ASTRA INDUSTRIAL", "Capital Goods"),
    ("4146", "GAS", "Capital Goods"),
    ("1302", "BAWAN", "Capital Goods"),
    ("1303", "EIC", "Capital Goods"),
    ("4148", "ALWASAIL INDUSTRIAL", "Capital Goods"),
    ("4145", "OGC", "Capital Goods"),
    ("2040", "SAUDI CERAMICS", "Capital Goods"),
    ("2110", "SAUDI CABLE", "Capital Goods"),
    ("4144", "RAOOM", "Capital Goods"),
    ("2160", "AMIANTIT", "Capital Goods"),
    ("2320", "ALBABTAIN", "Capital Goods"),
    ("2370", "MESC", "Capital Goods"),
    ("4140", "SIECO", "Capital Goods"),
    ("4141", "ALOMRAN", "Capital Goods"),
    ("4142", "RIYADH CABLES", "Capital Goods"),
    ("1214", "SHAKER", "Capital Goods"),
    ("4110", "BATIC", "Capital Goods"),
    ("4147", "CGS", "Capital Goods"),
    # Commercial & Professional Services
    ("4270", "SPPC", "Commercial & Professional Svc"),
    ("6004", "CATRION", "Commercial & Professional Svc"),
    ("1832", "SADR", "Commercial & Professional Svc"),
    ("1831", "MAHARAH", "Commercial & Professional Svc"),
    ("1833", "ALMAWARID", "Commercial & Professional Svc"),
    ("1834", "SMASCO", "Commercial & Professional Svc"),
    ("1835", "TAMKEEN", "Commercial & Professional Svc"),
    # Transportation
    ("4031", "SGS", "Transportation"),
    ("4040", "SAPTCO", "Transportation"),
    ("4260", "BUDGET SAUDI", "Transportation"),
    ("2190", "SISCO HOLDING", "Transportation"),
    ("4261", "THEEB", "Transportation"),
    ("4263", "SAL", "Transportation"),
    ("4262", "LUMI", "Transportation"),
    ("4265", "CHERRY", "Transportation"),
    ("4264", "FLYNAS", "Transportation"),
    # Consumer Durables & Apparel
    ("1213", "NASEEJ", "Consumer Durables & Apparel"),
    ("2130", "SIDC", "Consumer Durables & Apparel"),
    ("2340", "ARTEX", "Consumer Durables & Apparel"),
    ("4011", "LAZURDE", "Consumer Durables & Apparel"),
    ("4180", "FITAIHI GROUP", "Consumer Durables & Apparel"),
    ("4012", "ALASEEL", "Consumer Durables & Apparel"),
    # Consumer Services
    ("1810", "SEERA", "Consumer Services"),
    ("6013", "DWF", "Consumer Services"),
    ("1820", "BAAN", "Consumer Services"),
    ("4170", "TECO", "Consumer Services"),
    ("4290", "ALKHALEEJ TRNG", "Consumer Services"),
    ("6017", "JAHEZ", "Consumer Services"),
    ("6002", "HERFY FOODS", "Consumer Services"),
    ("1830", "LEEJAM SPORTS", "Consumer Services"),
    ("6012", "RAYDAN", "Consumer Services"),
    ("4291", "NCLE", "Consumer Services"),
    ("4292", "ATAA", "Consumer Services"),
    ("6014", "ALAMAR", "Consumer Services"),
    ("6015", "AMERICANA", "Consumer Services"),
    ("6016", "BURGERIZZR", "Consumer Services"),
    ("6018", "SPORT CLUBS", "Consumer Services"),
    ("6019", "ALMASAR ALSHAMIL", "Consumer Services"),
    ("6022", "ARMAH", "Consumer Services"),
    # Media and Entertainment
    ("4070", "TAPRCO", "Media and Entertainment"),
    ("4210", "SRMG", "Media and Entertainment"),
    ("4071", "ALARABIA", "Media and Entertainment"),
    ("4072", "MBC GROUP", "Media and Entertainment"),
    # Consumer Discretionary Distribution & Retail
    ("4003", "EXTRA", "Consumer Discretionary Distribution & Retail"),
    ("4008", "SACO", "Consumer Discretionary Distribution & Retail"),
    ("4050", "SASCO", "Consumer Discretionary Distribution & Retail"),
    ("4190", "JARIR", "Consumer Discretionary Distribution & Retail"),
    ("4240", "CENOMI RETAIL", "Consumer Discretionary Distribution & Retail"),
    ("4191", "ABO MOATI", "Consumer Discretionary Distribution & Retail"),
    ("4051", "BAAZEEM", "Consumer Discretionary Distribution & Retail"),
    ("4192", "ALSAIF GALLERY", "Consumer Discretionary Distribution & Retail"),
    ("4193", "NICE ONE", "Consumer Discretionary Distribution & Retail"),
    ("4194", "BUILD STATION", "Consumer Discretionary Distribution & Retail"),
    ("4200", "ALDREES", "Consumer Discretionary Distribution & Retail"),
    # Consumer Staples Distribution & Retail
    ("4001", "A.OTHAIM MARKET", "Consumer Staples Distribution & Retail"),
    ("4006", "FARM SUPERSTORES", "Consumer Staples Distribution & Retail"),
    ("4061", "ANAAM HOLDING", "Consumer Staples Distribution & Retail"),
    ("4160", "THIMAR", "Consumer Staples Distribution & Retail"),
    ("4161", "BINDAWOOD", "Consumer Staples Distribution & Retail"),
    ("4162", "ALMUNAJEM", "Consumer Staples Distribution & Retail"),
    ("4164", "NAHDI", "Consumer Staples Distribution & Retail"),
    ("4163", "ALDAWAA", "Consumer Staples Distribution & Retail"),
    # Food & Beverages
    ("2050", "SAVOLA GROUP", "Food & Beverages"),
    ("2100", "WAFRAH", "Food & Beverages"),
    ("2270", "SADAFCO", "Food & Beverages"),
    ("2280", "ALMARAI", "Food & Beverages"),
    ("6001", "HB", "Food & Beverages"),
    ("2288", "NOFOTH", "Food & Beverages"),
    ("6010", "NADEC", "Food & Beverages"),
    ("6020", "GACO", "Food & Beverages"),
    ("6040", "TADCO", "Food & Beverages"),
    ("6050", "SFICO", "Food & Beverages"),
    ("6060", "SHARQIYAH DEV", "Food & Beverages"),
    ("6070", "ALJOUF", "Food & Beverages"),
    ("6090", "JAZADCO", "Food & Beverages"),
    ("2281", "TANMIAH", "Food & Beverages"),
    ("2282", "NAQI", "Food & Beverages"),
    ("2283", "FIRST MILLS", "Food & Beverages"),
    ("4080", "SINAD HOLDING", "Food & Beverages"),
    ("2284", "MODERN MILLS", "Food & Beverages"),
    ("2285", "ARABIAN MILLS", "Food & Beverages"),
    ("2286", "FOURTH MILLING", "Food & Beverages"),
    ("2287", "ENTAJ", "Food & Beverages"),
    # Health Care Equipment & Services
    ("4002", "MOUWASAT", "Health Care Equipment & Svc"),
    ("4021", "CMCER", "Health Care Equipment & Svc"),
    ("4004", "DALLAH HEALTH", "Health Care Equipment & Svc"),
    ("4005", "CARE", "Health Care Equipment & Svc"),
    ("4007", "ALHAMMADI", "Health Care Equipment & Svc"),
    ("4009", "SAUDI GERMAN HEALTH", "Health Care Equipment & Svc"),
    ("2230", "CHEMICAL", "Health Care Equipment & Svc"),
    ("4013", "SULAIMAN ALHABIB", "Health Care Equipment & Svc"),
    ("2140", "AYYAN", "Health Care Equipment & Svc"),
    ("4014", "EQUIPMENT HOUSE", "Health Care Equipment & Svc"),
    ("4017", "FAKEEH CARE", "Health Care Equipment & Svc"),
    ("4018", "ALMOOSA", "Health Care Equipment & Svc"),
    ("4019", "SMC HEALTHCARE", "Health Care Equipment & Svc"),
    # Pharma, Biotech & Life Sciences
    ("2070", "SPIMACO", "Pharma, Biotech & Life Sciences"),
    ("4015", "JAMJOOM PHARMA", "Pharma, Biotech & Life Sciences"),
    ("4016", "AVALON PHARMA", "Pharma, Biotech & Life Sciences"),
    # Banks
    ("1010", "RIBL", "Banks"),
    ("1020", "BJAZ", "Banks"),
    ("1030", "SAIB", "Banks"),
    ("1050", "BSF", "Banks"),
    ("1060", "SAB", "Banks"),
    ("1080", "ANB", "Banks"),
    ("1120", "ALRAJHI", "Banks"),
    ("1140", "ALBILAD", "Banks"),
    ("1150", "ALINMA", "Banks"),
    ("1180", "SNB", "Banks"),
    # Financial Services
    ("2120", "SAIC", "Financial Services"),
    ("4280", "KINGDOM", "Financial Services"),
    ("4130", "SAUDI DARB", "Financial Services"),
    ("4081", "NAYIFAT", "Financial Services"),
    ("1111", "TADAWUL GROUP", "Financial Services"),
    ("4082", "MRNA", "Financial Services"),
    ("1182", "AMLAK", "Financial Services"),
    ("1183", "SHL", "Financial Services"),
    ("4083", "TASHEEL", "Financial Services"),
    ("4084", "DERAYAH", "Financial Services"),
    # Insurance
    ("8010", "TAWUNIYA", "Insurance"),
    ("8012", "JAZIRA TAKAFUL", "Insurance"),
    ("8020", "MALATH INSURANCE", "Insurance"),
    ("8030", "MEDGULF", "Insurance"),
    ("8040", "MUTAKAMELA", "Insurance"),
    ("8050", "SALAMA", "Insurance"),
    ("8060", "WALAA", "Insurance"),
    ("8070", "ARABIAN SHIELD", "Insurance"),
    ("8190", "UCA", "Insurance"),
    ("8230", "ALRAJHI TAKAFUL", "Insurance"),
    ("8280", "LIVA", "Insurance"),
    ("8150", "ACIG", "Insurance"),
    ("8210", "BUPA ARABIA", "Insurance"),
    ("8180", "ALSAGR INSURANCE", "Insurance"),
    ("8170", "ALETIHAD", "Insurance"),
    ("8100", "SAICO", "Insurance"),
    ("8120", "GULF UNION ALAHLIA", "Insurance"),
    ("8200", "SAUDI RE", "Insurance"),
    ("8160", "AICC", "Insurance"),
    ("8250", "GIG", "Insurance"),
    ("8240", "CHUBB", "Insurance"),
    ("8260", "GULF GENERAL", "Insurance"),
    ("8300", "WATANIYA", "Insurance"),
    ("8310", "AMANA INSURANCE", "Insurance"),
    ("8311", "ENAYA", "Insurance"),
    ("8313", "RASAN", "Insurance"),
    # Telecommunication Services
    ("7010", "STC", "Telecommunication Services"),
    ("7020", "ETIHAD ETISALAT", "Telecommunication Services"),
    ("7030", "ZAIN KSA", "Telecommunication Services"),
    ("7040", "GO TELECOM", "Telecommunication Services"),
    # Utilities
    ("2080", "GASCO HOLDING", "Utilities"),
    ("5110", "SAUDI ENERGY", "Utilities"),
    ("2081", "AWPT", "Utilities"),
    ("2082", "ACWA", "Utilities"),
    ("2083", "MARAFIQ", "Utilities"),
    ("2084", "MIAHONA", "Utilities"),
    # Real Estate Management & Development
    ("4020", "ALAKARIA", "Real Estate Mgmt & Dev't"),
    ("4324", "BANAN", "Real Estate Mgmt & Dev't"),
    ("4328", "LADUN", "Real Estate Mgmt & Dev't"),
    ("4323", "SUMOU", "Real Estate Mgmt & Dev't"),
    ("4090", "TAIBA", "Real Estate Mgmt & Dev't"),
    ("4100", "MCDC", "Real Estate Mgmt & Dev't"),
    ("4150", "ARDCO", "Real Estate Mgmt & Dev't"),
    ("4220", "EMAAR EC", "Real Estate Mgmt & Dev't"),
    ("4230", "RED SEA", "Real Estate Mgmt & Dev't"),
    ("4250", "JABAL OMAR", "Real Estate Mgmt & Dev't"),
    ("4300", "DAR ALARKAN", "Real Estate Mgmt & Dev't"),
    ("4310", "KEC", "Real Estate Mgmt & Dev't"),
    ("4320", "ALANDALUS", "Real Estate Mgmt & Dev't"),
    ("4321", "CENOMI CENTERS", "Real Estate Mgmt & Dev't"),
    ("4322", "RETAL", "Real Estate Mgmt & Dev't"),
    ("4326", "ALMAJDIAH", "Real Estate Mgmt & Dev't"),
    ("4325", "MASAR", "Real Estate Mgmt & Dev't"),
    ("4327", "AlRAMZ", "Real Estate Mgmt & Dev't"),
    # Software & Services
    ("7201", "ARAB SEA", "Software & Services"),
    ("7211", "AZM", "Software & Services"),
    ("7200", "MIS", "Software & Services"),
    ("7202", "SOLUTIONS", "Software & Services"),
    ("7203", "ELM", "Software & Services"),
    ("7204", "2P", "Software & Services"),
    ("7205", "DBS", "Software & Services"),
    # Household & Personal Products
    ("4165", "ALMAJED OUD", "Household & Personal Products"),
]

# Snapshot of tickers already live in core.companies as of this script's
# writing (2026-09-01) — see the module docstring's STALE RISK note for
# why this is a snapshot, not a live lookup, and why the generated SQL's
# WHERE NOT EXISTS guard (not this constant) is the actual safety net.
EXISTING_TICKERS: set[str] = {"2010", "2290", "2330"}

# Tickers present in RAW_COMPANIES whose name/sector should NOT be
# trusted enough to insert (none currently) — kept as an explicit,
# empty-by-default mechanism rather than omitted, so a future paste can
# flag an entry without silently deleting it from RAW_COMPANIES.
AMBIGUOUS_TICKERS: set[str] = set()

OUTPUT_SQL_PATH = "tadawul_companies_insert_statements.sql"


def _sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(value)


def check_no_duplicate_tickers(companies: list[tuple[str, str, str]]) -> None:
    """Raises if RAW_COMPANIES itself contains a duplicate ticker — a
    transcription error, never silently deduped."""
    seen: set[str] = set()
    dupes: set[str] = set()
    for ticker, _name, _sector in companies:
        if ticker in seen:
            dupes.add(ticker)
        seen.add(ticker)
    if dupes:
        raise ValueError(f"duplicate ticker(s) found in RAW_COMPANIES (transcription error): {sorted(dupes)}")


def filter_companies(
    companies: list[tuple[str, str, str]],
    existing_tickers: set[str],
    ambiguous_tickers: set[str],
) -> tuple[list[tuple[str, str, str]], list[str], list[str]]:
    """Pure, offline-testable. Returns (kept, skipped_existing, skipped_ambiguous)."""
    kept: list[tuple[str, str, str]] = []
    skipped_existing: list[str] = []
    skipped_ambiguous: list[str] = []
    for ticker, name, sector in companies:
        if ticker in existing_tickers:
            skipped_existing.append(ticker)
            continue
        if ticker in ambiguous_tickers:
            skipped_ambiguous.append(ticker)
            continue
        kept.append((ticker, name, sector))
    return kept, skipped_existing, skipped_ambiguous


def build_insert_sql(companies: list[tuple[str, str, str]]) -> list[str]:
    """One INSERT per company, guarded by WHERE NOT EXISTS against
    core.companies.ticker (see module docstring for why NOT ON CONFLICT:
    core.companies has no real UNIQUE/exclusion constraint on ticker).
    name_ar is explicitly NULL — not available from this source, never
    invented."""
    statements: list[str] = []
    for ticker, name_en, sector in companies:
        stmt = (
            "INSERT INTO core.companies (company_id, ticker, name_ar, name_en, sector, status, created_at, updated_at)\n"
            "SELECT gen_random_uuid(), "
            f"{_sql_literal(ticker)}, NULL, {_sql_literal(name_en)}, {_sql_literal(sector)}, 'active', now(), now()\n"
            "WHERE NOT EXISTS (\n"
            f"    SELECT 1 FROM core.companies WHERE ticker = {_sql_literal(ticker)}\n"
            ");"
        )
        statements.append(stmt)
    return statements


def main() -> None:
    print("=" * 78)
    print("DISCOVER TADAWUL COMPANIES — metadata only (ticker, name_en, sector), no PDFs")
    print("No Neon connection, no network access — this script is fully offline.")
    print("=" * 78)

    try:
        check_no_duplicate_tickers(RAW_COMPANIES)
    except ValueError as e:
        print(f"REFUSED: {e}", file=__import__("sys").stderr)
        raise SystemExit(1)

    kept, skipped_existing, skipped_ambiguous = filter_companies(
        RAW_COMPANIES, EXISTING_TICKERS, AMBIGUOUS_TICKERS
    )

    print(f"RAW_COMPANIES total (as transcribed): {len(RAW_COMPANIES)}")
    print(f"Skipped (already in core.companies, per EXISTING_TICKERS snapshot): {sorted(skipped_existing)}")
    if skipped_ambiguous:
        print(f"WARNING — skipped (flagged ambiguous, not inserted): {sorted(skipped_ambiguous)}")
    print(f"Companies to insert: {len(kept)}")

    statements = build_insert_sql(kept)
    with open(OUTPUT_SQL_PATH, "w", encoding="utf-8") as f:
        f.write(
            "-- tadawul_companies_insert_statements.sql\n"
            "-- Generated by scripts/discover_tadawul_companies.py — NOT executed against\n"
            "-- Neon by this script.\n"
            "-- Source: a list the project owner states they fetched live from\n"
            "-- argaam.com/en on 2026-09-01, pasted into this session as raw text and\n"
            "-- transcribed here — NOT independently fetched or verified by this script\n"
            "-- (WebFetch was confirmed fully blocked in the authoring sandbox).\n"
            "-- name_ar is NULL for every row — not available from this source.\n"
            "-- Each INSERT is guarded by WHERE NOT EXISTS (core.companies has no real\n"
            "-- UNIQUE/exclusion constraint on ticker to target with ON CONFLICT — verified\n"
            "-- against schema.sql this session).\n"
            "-- Review before running manually (e.g. via Neon SQL Editor / Render Shell).\n\n"
        )
        f.write(f"-- {len(statements)} companies\n\n")
        f.write("\n\n".join(statements))
        f.write("\n")

    print()
    print("=" * 78)
    print(f"SQL written to: {OUTPUT_SQL_PATH}")
    print("NEON WRITES ISSUED BY THIS SCRIPT: 0")
    print("=" * 78)


if __name__ == "__main__":
    main()
