"""H2 store migration: v1 → v2 upgrades additively with an automatic
premigrate backup; downgrade (newer store, older code) is refused."""
from __future__ import annotations

import pytest

from app.health import config, schema, store


def _make_v1_store(home_env):
    """Fresh store, then strip it back to schema v1 (drop post-v1 tables)."""
    conn = store.connect(create=True)
    conn.execute("DROP TABLE events")
    conn.execute("DROP TABLE documents")
    conn.execute("UPDATE schema_meta SET value='1' WHERE key='schema_version'")
    conn.close()


def test_fresh_store_is_current_version(health_home):
    conn = store.connect(create=True)
    try:
        assert store.counts(conn)["schema_version"] == schema.SCHEMA_VERSION
        assert store.counts(conn)["events"] == 0
    finally:
        conn.close()


def test_v1_store_migrates_with_premigrate_backup(health_home):
    _make_v1_store(health_home)
    backups_dir = config.resolve_home() / "backups"
    assert list(backups_dir.iterdir()) == []

    conn = store.connect()          # reopening triggers the migration
    try:
        info = store.counts(conn)
    finally:
        conn.close()
    assert info["schema_version"] == schema.SCHEMA_VERSION
    assert info["events"] == 0      # table exists, empty
    assert info["documents"] == 0

    (backup,) = list(backups_dir.iterdir())
    assert backup.name.startswith(
        f"health.duckdb.premigrate-v1-to-v{schema.SCHEMA_VERSION}-")


def test_migration_preserves_existing_rows(health_home, catalog_dir,
                                           labs_csv_path):
    from app.health.importers import labs_csv

    labs_csv.run(labs_csv_path, catalog_dir=catalog_dir)
    _make_v1_store(health_home)     # strip back down, keeping observations
    conn = store.connect()
    try:
        info = store.counts(conn)
    finally:
        conn.close()
    assert info["schema_version"] == schema.SCHEMA_VERSION
    assert info["observations"] == 10


def test_newer_store_is_refused(health_home):
    conn = store.connect(create=True)
    conn.execute("UPDATE schema_meta SET value='99' WHERE key='schema_version'")
    conn.close()
    with pytest.raises(RuntimeError, match="newer than supported"):
        store.connect()


def test_v2_store_migrates_to_v3_documents(health_home):
    """A v2 store (events but no documents) gains the documents table on
    reopen, with a premigrate backup."""
    conn = store.connect(create=True)
    conn.execute("DROP TABLE documents")
    conn.execute("UPDATE schema_meta SET value='2' WHERE key='schema_version'")
    conn.close()

    conn = store.connect()
    try:
        info = store.counts(conn)
    finally:
        conn.close()
    assert info["schema_version"] == 3
    assert info["documents"] == 0
    backups = list((config.resolve_home() / "backups").iterdir())
    assert any(b.name.startswith("health.duckdb.premigrate-v2-to-v3-")
               for b in backups)
