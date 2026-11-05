"""DuckDB store. Columnar because Stage 4 scans hundreds of millions of rows.

Provenance is not optional: every price row carries source file hash, byte
offset, parser dialect, payer rule id, and download date.
"""
from __future__ import annotations
import duckdb
from .config import DB_PATH

SCHEMA = """
-- Stage 1: sample frame -----------------------------------------------------
CREATE TABLE IF NOT EXISTS hospital (
  ccn                TEXT PRIMARY KEY,
  name               TEXT,
  dba_name           TEXT,
  address            TEXT,
  city               TEXT,
  state              TEXT,
  zip                TEXT,
  county             TEXT,
  provider_category  TEXT,   -- short_term_acute|critical_access|childrens|psychiatric|rehab|ltch
  ownership_raw      TEXT,
  ownership          TEXT,   -- nonprofit|forprofit|government|other
  beds               INTEGER,
  system_name        TEXT,
  system_id          TEXT,
  cbsa               TEXT,
  rural              BOOLEAN,
  source_dataset     TEXT,
  source_sha256      TEXT,
  retrieved_at       TIMESTAMP
);

-- Stage 1b: hospital -> web domain (no CMS file contains this) --------------
CREATE TABLE IF NOT EXISTS hospital_domain (
  ccn                TEXT,
  domain             TEXT,
  resolution_method  TEXT,   -- system_map|search|manual|aggregator
  confidence         TEXT,   -- certain|probable|ambiguous
  shared_with_n      INTEGER,-- how many CCNs share this domain
  note               TEXT,
  resolved_at        TIMESTAMP,
  PRIMARY KEY (ccn, domain)
);

-- Stage 2: discovery --------------------------------------------------------
CREATE TABLE IF NOT EXISTS discovery (
  ccn                TEXT,
  url                TEXT,
  url_found          BOOLEAN,
  discovery_method   TEXT,   -- cms_hpt_txt|footer|sitemap|aggregator|manual
  cms_hpt_present    BOOLEAN,
  cms_hpt_wellformed BOOLEAN,
  cms_hpt_fields     TEXT,   -- JSON of parsed fields
  http_status        INTEGER,
  content_type       TEXT,
  wall_type          TEXT,   -- none|login|captcha|clickthrough|js_render
  attempted_at       TIMESTAMP,
  note               TEXT
);

-- Stage 2: acquisition ------------------------------------------------------
CREATE TABLE IF NOT EXISTS mrf_file (
  file_id            TEXT PRIMARY KEY,   -- sha256 of uncompressed bytes
  ccn                TEXT,
  url                TEXT,
  http_status        INTEGER,
  content_type       TEXT,
  file_size_bytes    BIGINT,
  sha256             TEXT,               -- of uncompressed stream
  sha256_archive     TEXT,               -- of the zstd artifact, if archived
  archived_path      TEXT,               -- NULL under stream-only policy
  download_timestamp TIMESTAMP,
  dialect            TEXT,
  encoding           TEXT,
  parse_status       TEXT,               -- ok|partial|failed|not_machine_readable
  parse_error        TEXT,
  rows_scanned       BIGINT,
  storage_policy     TEXT,
  cms_schema_version TEXT                -- archive|stream_only
);

-- One file can cover many hospitals (system files). Attribution is explicit.
CREATE TABLE IF NOT EXISTS file_hospital_attribution (
  file_id            TEXT,
  ccn                TEXT,
  attribution_basis  TEXT,   -- ccn_in_file|ein_in_file|name_in_file|domain_only
  named_in_file      BOOLEAN,
  PRIMARY KEY (file_id, ccn)
);

-- Stage 3: compliance -------------------------------------------------------
CREATE TABLE IF NOT EXISTS compliance (
  ccn TEXT, file_id TEXT,
  r1_retrievable BOOLEAN, r2_cms_hpt BOOLEAN, r3_machine_readable BOOLEAN,
  r4_gross BOOLEAN, r5_cash BOOLEAN, r6_named_payers BOOLEAN,
  r7_minmax BOOLEAN, r8_codes_typed BOOLEAN, r9_cms_schema BOOLEAN,
  r10_no_wall BOOLEAN,
  score INTEGER,
  anonymized_payers BOOLEAN,       -- distinct evasion category, counted separately
  materially_compliant BOOLEAN,
  strict_compliant BOOLEAN,
  lenient_compliant BOOLEAN,
  attribution_rule TEXT,           -- named_in_file|domain_credit
  scored_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS compliance_manual (
  ccn TEXT PRIMARY KEY, score INTEGER, elements TEXT, reviewer TEXT, reviewed_at TIMESTAMP
);

-- Stage 4: extracted prices -------------------------------------------------
CREATE TABLE IF NOT EXISTS price_raw (
  row_id BIGINT, file_id TEXT, ccn TEXT,
  service_key TEXT, code TEXT, code_type TEXT, setting TEXT,
  payer_string_raw TEXT,          -- verbatim, never overwritten
  plan_string_raw TEXT,
  rate_type TEXT,                 -- negotiated|gross|cash|min|max
  rate_dollars DOUBLE,            -- NULL when not a dollar figure
  rate_percent DOUBLE,
  estimated_allowed_amount DOUBLE,
  methodology TEXT,               -- fee_schedule|percent_of_charge|per_diem|case_rate|other
  billing_class TEXT,             -- facility|professional|both
  byte_offset BIGINT,
  evidence_excerpt TEXT,
  parser_dialect TEXT,
  source_sha256 TEXT,
  download_date DATE
);

-- Stage 4b: payer normalization --------------------------------------------
CREATE TABLE IF NOT EXISTS payer_string (
  payer_string_raw TEXT PRIMARY KEY,
  n_rows BIGINT, n_hospitals INTEGER,
  parent_entity TEXT, brand TEXT, product_line TEXT,
  plan_type TEXT,                 -- commercial|medicare_advantage|medicaid_mco|other|unknown
  network TEXT,
  anonymized BOOLEAN,
  rule_id TEXT,                   -- which deterministic rule matched
  confidence TEXT,                -- certain|probable|ambiguous|unresolved
  resolved_by TEXT                -- rule|manual
);

CREATE TABLE IF NOT EXISTS payer_review_queue (
  payer_string_raw TEXT, candidate_parent TEXT, proposer_score DOUBLE,
  n_rows BIGINT, n_hospitals INTEGER, reason TEXT, status TEXT, queued_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS plan_type_flag (
  payer_string_raw TEXT, ccn TEXT, service_key TEXT,
  assigned_plan_type TEXT, rate_dollars DOUBLE, medicare_rate DOUBLE,
  ratio_to_medicare DOUBLE, flag_reason TEXT, resolution TEXT
);

-- Reference -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS medicare_rate (
  code TEXT, code_type TEXT, setting TEXT, year INTEGER,
  national_rate DOUBLE, wage_adjusted_rate DOUBLE, cbsa TEXT,
  source TEXT, source_sha256 TEXT
);

CREATE TABLE IF NOT EXISTS service_offered (
  ccn TEXT, service_key TEXT,
  offered TEXT,          -- yes|no|undeterminable
  basis TEXT,            -- external evidence ONLY; never MRF silence
  source TEXT
);

CREATE TABLE IF NOT EXISTS market_hhi (
  geography_type TEXT, geography TEXT, hhi DOUBLE, n_hospitals INTEGER,
  basis TEXT             -- staffed_bed_share (proxy; not discharge-weighted)
);

-- Provenance ledger: every external download ever made ----------------------
CREATE TABLE IF NOT EXISTS download_log (
  url TEXT, sha256 TEXT, bytes BIGINT, http_status INTEGER,
  content_type TEXT, retrieved_at TIMESTAMP, cache_path TEXT, stage TEXT
);

CREATE TABLE IF NOT EXISTS attrition (
  stage TEXT, step TEXT, n INTEGER, denominator INTEGER, note TEXT, recorded_at TIMESTAMP
);
"""


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(DB_PATH), read_only=read_only)
    if not read_only:
        con.execute(SCHEMA)
    return con


def record_attrition(con, stage: str, step: str, n: int, denominator: int | None = None,
                     note: str = "") -> None:
    con.execute(
        "INSERT INTO attrition VALUES (?,?,?,?,?,now())",
        [stage, step, n, denominator, note],
    )
