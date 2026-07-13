"""DuckDB-backed health store (H-D1: independent from cairn.db).

DuckDB was confirmed against the packaging constraints in H0 (prebuilt
arm64/x86 wheels, no build step, ~40MB): the ADR-preferred choice, so the
SQLite fallback path was not taken. duckdb is imported lazily so `cairn
--help` and the non-health test suite never pay the import.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import config, schema

logger = logging.getLogger("cairn.health")


def _peek_version(path: Path) -> int | None:
    """Read schema_version without taking a write handle (for the
    premigrate backup decision — the copy must precede any write)."""
    import duckdb

    ro = duckdb.connect(str(path), read_only=True)
    try:
        row = ro.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        return int(row[0]) if row else None
    except duckdb.CatalogException:
        return None
    finally:
        ro.close()


def _premigrate_backup(home: Path, path: Path, from_version: int) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = (home / "backups" /
              f"health.duckdb.premigrate-v{from_version}-to-"
              f"v{schema.SCHEMA_VERSION}-{stamp}")
    shutil.copy2(path, target)
    config.protect_file(target)
    logger.info("health store premigrate backup v%d->v%d",
                from_version, schema.SCHEMA_VERSION)
    return target


def connect(home: Path | None = None, *, create: bool = False):
    """Open (and on ``create=True`` initialize) the health store.

    Applies/validates the schema version — upgrading an older store first
    snapshots the DB file into backups/ (additive migrations only, but the
    premigrate copy makes even those trivially reversible). Enforces file
    mode 0600. Returns a duckdb connection.
    """
    import duckdb

    if home is None:
        home = config.ensure_home() if create else config.resolve_home()
    path = config.store_path(home)
    if not create and not path.exists():
        raise FileNotFoundError(
            f"health store not initialized (run `cairn health init`): {path}"
        )
    if path.exists():
        current = _peek_version(path)
        if current is not None and current < schema.SCHEMA_VERSION:
            _premigrate_backup(home, path, current)
    conn = duckdb.connect(str(path))
    schema.apply(conn)
    config.protect_file(path)
    return conn


def connect_readonly(home: Path | None = None):
    """Open the store READ-ONLY, without migrating.

    For read-only consumers (the health MCP server, H7): a read-only handle
    coexists with other read-only readers and never writes, so it can run
    beside the CLI's occasional access without fighting over the single
    writer. It refuses to migrate — if the store is behind, the user must run
    a ``cairn health`` command first (writes belong to that path, not here).
    """
    import duckdb

    home = home or config.resolve_home()
    path = config.store_path(home)
    if not path.exists():
        raise FileNotFoundError(
            f"health store not initialized (run `cairn health init`): {path}")
    conn = duckdb.connect(str(path), read_only=True)
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        current = int(row[0]) if row else 0
    except Exception:
        conn.close()
        raise RuntimeError(
            "health store schema is unavailable; run `cairn health doctor`"
            " locally") from None
    if current != schema.SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(
            f"health store is schema v{current}, code expects "
            f"v{schema.SCHEMA_VERSION}; run any `cairn health` command to "
            "migrate before read-only access")
    return conn


def counts(conn) -> dict:
    """Row counts for status/doctor output — counts and versions only, no
    metric names, dates or values (PRIVACY.md §5)."""
    out: dict = {}
    for table in (
        "source_files",
        "import_runs",
        "observations",
        "events",
        "documents",
        "interpretations",
        "interpretation_evidence",
        "data_snapshots",
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
