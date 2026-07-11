"""DuckDB-backed health store (H-D1: independent from cairn.db).

DuckDB was confirmed against the packaging constraints in H0 (prebuilt
arm64/x86 wheels, no build step, ~40MB): the ADR-preferred choice, so the
SQLite fallback path was not taken. duckdb is imported lazily so `cairn
--help` and the non-health test suite never pay the import.
"""
from __future__ import annotations

from pathlib import Path

from . import config, schema


def connect(home: Path | None = None, *, create: bool = False):
    """Open (and on ``create=True`` initialize) the health store.

    Always applies/validates the schema version and enforces file mode 0600
    on the DB file. Returns a duckdb connection.
    """
    import duckdb

    if home is None:
        home = config.ensure_home() if create else config.resolve_home()
    path = config.store_path(home)
    if not create and not path.exists():
        raise FileNotFoundError(
            f"health store not initialized (run `cairn health init`): {path}"
        )
    conn = duckdb.connect(str(path))
    schema.apply(conn)
    config.protect_file(path)
    return conn


def counts(conn) -> dict:
    """Row counts for status/doctor output — counts and versions only, no
    metric names, dates or values (PRIVACY.md §5)."""
    out: dict = {}
    for table in (
        "source_files",
        "import_runs",
        "observations",
        "quarantine_records",
        "metric_catalog",
        "metric_aliases",
    ):
        out[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    out["schema_version"] = int(
        conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
    )
    return out
