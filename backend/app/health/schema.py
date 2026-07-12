"""Health store schema (docs/health/DATA_MODEL.md), H0/H1 + H2 subset.

Versioned independently from cairn.db (schema_meta.schema_version, additive
migrations with an automatic premigrate backup). Future tables (documents /
interpretations / data_snapshots) are NOT created in advance —
H0_H1_TASK.md forbids pre-building later milestones.

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

SCHEMA_VERSION = 3

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

# v2 (H2): explicit intervention/context event ledger. Times may be exact,
# month-only or approximate — represented as the ORIGINAL string plus an
# earliest/latest DATE interval, never an invented timestamp. Rows are
# append-only; a correction is a NEW row pointing at its predecessor via
# supersedes_id (ACCEPTANCE H2).
EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS events (
    id             TEXT PRIMARY KEY,            -- author-assigned, stable
    kind           TEXT NOT NULL,
    label          TEXT,
    start_raw      TEXT,                        -- verbatim (e.g. '2031-03', '~2031-06-01')
    start_earliest DATE,
    start_latest   DATE,
    end_raw        TEXT,
    end_earliest   DATE,
    end_latest     DATE,
    time_precision TEXT NOT NULL,               -- date / month / approximate / unknown
    status         TEXT NOT NULL,               -- active / completed / uncertain
    dose_value     DOUBLE,
    dose_unit      TEXT,
    route          TEXT,
    frequency      TEXT,
    source_type    TEXT NOT NULL,               -- self_report / clinician / document
    source_file_id TEXT,
    confidence     TEXT NOT NULL,               -- confirmed / estimated / uncertain
    notes          TEXT,                        -- free text; NEVER auto-interpreted
    supersedes_id  TEXT,                        -- append-only correction chain
    entry_hash     TEXT NOT NULL,               -- content hash for idempotency
    imported_at    TIMESTAMPTZ NOT NULL,
    meta_json      TEXT
);
"""

# v3 (H4): medical document registry. The document file is snapshotted
# immutably into raw/ (source_files carries hash/size/kind); this row adds
# clinical metadata and an extraction lifecycle. extraction_status is
# 'none' at import and only becomes 'verified' by an explicit human action —
# OCR/extracted text is never silently trusted (ACCEPTANCE H4).
DOCUMENTS_DDL = """
CREATE TABLE IF NOT EXISTS documents (
    id                  TEXT PRIMARY KEY,
    document_kind       TEXT NOT NULL,          -- lab_report / imaging / endoscopy / prescription ...
    title               TEXT NOT NULL,
    document_date       DATE,
    source_file_id      TEXT NOT NULL,          -- immutable raw snapshot
    issuer              TEXT,
    extracted_text_path TEXT,                   -- relative to the data home
    extraction_status   TEXT NOT NULL DEFAULT 'none',  -- none / draft / verified
    imported_at         TIMESTAMPTZ NOT NULL,
    meta_json           TEXT
);
"""

# version -> DDL applied when upgrading TO that version (additive only).
MIGRATIONS: dict[int, str] = {2: EVENTS_DDL, 3: DOCUMENTS_DDL}


def apply(conn) -> None:
    """Create/upgrade the schema and stamp the version (idempotent).

    Additive migrations only. The premigrate file backup happens BEFORE the
    store is even opened (store.connect peeks at the version read-only) —
    by the time this runs, the snapshot already exists.
    """
    conn.execute(DDL)  # duckdb runs multi-statement scripts natively
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        # Fresh store: base DDL + every migration = current version.
        for version in sorted(MIGRATIONS):
            conn.execute(MIGRATIONS[version])
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
            [str(SCHEMA_VERSION)],
        )
        return

    current = int(row[0])
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"health store schema v{current} is newer than supported "
            f"v{SCHEMA_VERSION} — update the code, do not downgrade the store"
        )
    if current < SCHEMA_VERSION:
        for version in range(current + 1, SCHEMA_VERSION + 1):
            conn.execute(MIGRATIONS[version])
        conn.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_version'",
            [str(SCHEMA_VERSION)],
        )
