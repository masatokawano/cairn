"""Health store schema, H0/H1 subset only (docs/health/DATA_MODEL.md).

Versioned independently from cairn.db (schema_meta.schema_version). Future
tables (events / documents / interpretations / data_snapshots) are NOT
created in advance — H0_H1_TASK.md forbids pre-building later milestones.

Column notes:

- ids are uuid4 hex TEXT generated in Python (portable, no DB extension);
- ``*_json`` columns are TEXT to avoid a JSON-extension dependency;
- observations keeps the original fact even when normalization fails: for a
  numeric value with an unknown unit, ``value_num``/``unit`` stay NULL and
  the raw string is preserved in ``value_text`` with
  ``quality_status='provisional'`` (DESIGN.md health §6: 原値だけ保存し、
  normalized をNULLにする) — the CHECK below still holds because
  value_text is set.
"""
from __future__ import annotations

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_files (
    id                TEXT PRIMARY KEY,
    source_kind       TEXT NOT NULL,            -- labs_csv / apple_health / document / events
    original_name     TEXT NOT NULL,
    stored_path       TEXT NOT NULL,            -- relative to the data home
    sha256            TEXT NOT NULL UNIQUE,
    size_bytes        BIGINT NOT NULL,
    acquired_at       TIMESTAMPTZ NOT NULL,
    source_created_at TIMESTAMPTZ,
    parser_name       TEXT NOT NULL,
    parser_version    TEXT NOT NULL,
    status            TEXT NOT NULL,            -- imported / partial / quarantined / failed
    meta_json         TEXT
);

CREATE TABLE IF NOT EXISTS import_runs (
    id                    TEXT PRIMARY KEY,
    source_file_id        TEXT NOT NULL,
    started_at            TIMESTAMPTZ NOT NULL,
    completed_at          TIMESTAMPTZ,
    inserted              BIGINT NOT NULL DEFAULT 0,
    updated               BIGINT NOT NULL DEFAULT 0,
    skipped               BIGINT NOT NULL DEFAULT 0,
    quarantined           BIGINT NOT NULL DEFAULT 0,
    status                TEXT NOT NULL,        -- running / ok / partial / failed
    error_code            TEXT,                 -- machine-readable, no personal values
    error_detail_redacted TEXT
);

CREATE TABLE IF NOT EXISTS metric_catalog (
    metric_id            TEXT PRIMARY KEY,
    label_ja             TEXT NOT NULL,
    label_en             TEXT,
    quantity_kind        TEXT NOT NULL,
    canonical_unit       TEXT,
    loinc_code           TEXT,
    healthkit_identifier TEXT,
    catalog_version      TEXT NOT NULL,
    active               BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS metric_aliases (
    source_namespace TEXT NOT NULL,             -- lab_sheet / apple_health ...
    source_name      TEXT NOT NULL,             -- verbatim source spelling
    metric_id        TEXT NOT NULL,
    mapping_version  TEXT NOT NULL,
    confidence       TEXT NOT NULL,             -- confirmed / provisional
    note             TEXT,
    PRIMARY KEY (source_namespace, source_name)
);

CREATE TABLE IF NOT EXISTS observations (
    id              TEXT PRIMARY KEY,
    subject_id      TEXT NOT NULL,
    metric_id       TEXT,                       -- canonical; NULL when unmapped
    original_metric TEXT NOT NULL,
    value_num       DOUBLE,
    value_text      TEXT,
    unit            TEXT,                       -- canonical unit (normalized)
    original_value  TEXT NOT NULL,
    original_unit   TEXT,
    observed_start  TIMESTAMPTZ,
    observed_end    TIMESTAMPTZ,
    observed_date   DATE,
    time_precision  TEXT NOT NULL,              -- instant / interval / date / unknown
    specimen        TEXT,
    fasting_state   TEXT,
    reference_low   DOUBLE,
    reference_high  DOUBLE,
    reference_text  TEXT,
    flag_source     TEXT,
    source_name     TEXT NOT NULL,
    device_name     TEXT,
    source_file_id  TEXT NOT NULL,
    source_row_ref  TEXT,
    fingerprint     TEXT NOT NULL UNIQUE,
    mapping_version TEXT,
    quality_status  TEXT NOT NULL,              -- valid / provisional / quarantined
    meta_json       TEXT,
    CHECK (value_num IS NOT NULL OR value_text IS NOT NULL),
    CHECK (observed_start IS NOT NULL OR observed_end IS NOT NULL
           OR observed_date IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS quarantine_records (
    id              TEXT PRIMARY KEY,
    source_file_id  TEXT NOT NULL,
    import_run_id   TEXT NOT NULL,
    reason_code     TEXT NOT NULL,              -- unknown_metric / parse_error
    original_metric TEXT,
    original_unit   TEXT,
    source_row_ref  TEXT,
    payload_json    TEXT,                       -- original cells; protected store only
    created_at      TIMESTAMPTZ NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
);
"""


def apply(conn) -> None:
    """Create the H0/H1 schema and stamp the version (idempotent)."""
    conn.execute(DDL)  # duckdb runs multi-statement scripts natively
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
            [str(SCHEMA_VERSION)],
        )
    elif int(row[0]) != SCHEMA_VERSION:
        # Only v1 exists; a mismatch means a newer store touched by older code.
        raise RuntimeError(
            f"health store schema v{row[0]} != supported v{SCHEMA_VERSION}"
        )
