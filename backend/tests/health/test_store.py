"""H0 store: schema initialization, version gate, file protection."""
from __future__ import annotations

import stat

import pytest

from app.health import config, schema, store


def test_init_creates_store_with_schema_and_0600(health_home):
    conn = store.connect(create=True)
    info = store.counts(conn)
    conn.close()
    assert info["schema_version"] == schema.SCHEMA_VERSION
    assert info["observations"] == 0
    path = config.store_path(health_home.resolve())
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_connect_without_init_fails(health_home):
    config.ensure_home()
    with pytest.raises(FileNotFoundError, match="init"):
        store.connect()


def test_schema_version_mismatch_refused(health_home):
    conn = store.connect(create=True)
    conn.execute("UPDATE schema_meta SET value='99' WHERE key='schema_version'")
    conn.close()
    with pytest.raises(RuntimeError, match="v99"):
        store.connect()
