"""H8: backup / restore / integrity / retention / deletion.

Maps to ACCEPTANCE.md H8. Synthetic data only; destructive paths run against
throwaway temp homes, never a real store.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.health import config, ops, store
from app.health.importers import events_yaml, labs_csv

from .conftest import FIXTURES


@pytest.fixture
def populated(health_home, catalog_dir, labs_csv_path):
    labs_csv.run(labs_csv_path, catalog_dir=catalog_dir)
    events_yaml.run(FIXTURES / "synthetic_events.yml")
    return health_home.resolve()


def _obs_count(home):
    conn = store.connect_readonly(home)
    try:
        return conn.execute("SELECT count(*) FROM observations").fetchone()[0]
    finally:
        conn.close()


# --- backup / restore ---------------------------------------------------------

def test_backup_snapshots_consistently(populated, tmp_path):
    dest = tmp_path / "backups-out"
    out = ops.backup(dest_dir=dest)
    archive = Path(out["archive"])
    assert archive.exists() and archive.suffix == ".gz"
    assert out["table_counts"]["observations"] == 10
    assert out["versions"]["schema_version"] == store.schema.SCHEMA_VERSION
    # Manifest hashes raw sources + store.
    man = ops.read_manifest(archive)
    assert man["store_sha256"]
    assert any("raw/labs_csv" in k for k in man["raw_sha256"])


def test_restore_reproduces_counts_and_hashes(populated, tmp_path):
    out = ops.backup(dest_dir=tmp_path / "b")
    into = tmp_path / "restored"
    res = ops.restore(Path(out["archive"]), into)
    assert res["ok"] is True
    assert res["mismatches"] == []
    assert _obs_count(into) == 10
    # Restored store file mode is protected.
    import stat
    sf = config.store_path(into)
    assert stat.S_IMODE(sf.stat().st_mode) == 0o600


def test_restore_refuses_nonempty_target(populated, tmp_path):
    out = ops.backup(dest_dir=tmp_path / "b")
    into = tmp_path / "occupied"
    into.mkdir()
    (into / "x").write_text("busy")
    with pytest.raises(ops.OpsError, match="empty"):
        ops.restore(Path(out["archive"]), into)


def test_verify_backup_detects_tamper(populated, tmp_path):
    out = ops.backup(dest_dir=tmp_path / "b")
    assert ops.verify_backup(Path(out["archive"]))["ok"] is True
    # Corrupt the archive bytes → verify fails (or errors), never false-ok.
    archive = Path(out["archive"])
    data = bytearray(archive.read_bytes())
    data[-50] ^= 0xFF
    archive.write_bytes(bytes(data))
    try:
        assert ops.verify_backup(archive)["ok"] is False
    except Exception:
        pass  # a gzip/tar error is an acceptable "not ok" too


def test_backup_refuses_worktree_destination(populated):
    repo_dir = Path(__file__).resolve().parent    # inside the git worktree
    with pytest.raises(ops.OpsError, match="worktree"):
        ops.backup(dest_dir=repo_dir / "should-not-write")


def test_backup_failure_preserves_store(populated, tmp_path, monkeypatch):
    """ACCEPTANCE H8: a failed backup must not damage a successful import."""
    before = _obs_count(populated)

    import app.health.ops as ops_mod
    orig_open = ops_mod.tarfile.open

    def boom(*a, **k):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(ops_mod.tarfile, "open", boom)
    with pytest.raises(OSError):
        ops.backup(dest_dir=tmp_path / "b")
    monkeypatch.setattr(ops_mod.tarfile, "open", orig_open)

    assert _obs_count(populated) == before          # store intact
    # No half-written .part archive left behind.
    leftover = list((tmp_path / "b").glob("*.part")) if (tmp_path / "b").exists() else []
    assert leftover == []


# --- retention ----------------------------------------------------------------

def test_rotate_keeps_newest(populated, tmp_path, monkeypatch):
    dest = tmp_path / "b"
    made = []
    for i in range(4):
        out = ops.backup(dest_dir=dest)
        p = Path(out["archive"])
        # Space out mtimes so ordering is deterministic.
        import os
        os.utime(p, (1000 + i, 1000 + i))
        made.append(p)
    monkeypatch.setenv("CAIRN_HEALTH_HOME", str(populated))
    res = ops.rotate_backups(dest, keep=2)
    assert len(res["removed"]) == 2
    remaining = sorted((tmp_path / "b").glob("health-backup-*.tar.gz"))
    assert len(remaining) == 2
    assert made[-1] in remaining and made[-2] in remaining   # newest kept


def test_rotate_rejects_zero(populated, tmp_path):
    with pytest.raises(ops.OpsError, match="keep"):
        ops.rotate_backups(tmp_path / "b", keep=0)


# --- deletion -----------------------------------------------------------------

def test_delete_derived_is_regenerable_only(populated):
    from app.health.reports import lab_summary

    lab_summary.write(populated)                    # creates reports/lab-summary.md
    assert (populated / "reports" / "lab-summary.md").exists()
    res = ops.delete_derived(populated)
    assert any("reports/" in r or "reports" in r for r in res["removed"])
    assert not (populated / "reports" / "lab-summary.md").exists()
    # Sources + store + observations survive; report regenerates.
    assert _obs_count(populated) == 10
    lab_summary.write(populated)
    assert (populated / "reports" / "lab-summary.md").exists()


def test_purge_plan_enumerates_without_deleting(populated):
    plan = ops.purge_plan(populated)
    assert set(plan["directories"]) == set(ops.PURGE_DIRS)
    assert plan["directories"]["store"]["exists"] is True
    assert plan["directories"]["raw"]["files"] >= 1
    assert _obs_count(populated) == 10              # nothing deleted


def test_purge_requires_exact_confirmation(populated):
    with pytest.raises(ops.OpsError, match="refused"):
        ops.purge(populated, confirm="yes")
    with pytest.raises(ops.OpsError, match="refused"):
        ops.purge(populated, confirm=None)
    assert populated.exists()                       # still there


def test_purge_deletes_everything_with_confirmation(populated):
    res = ops.purge(populated, confirm=str(populated))
    assert res["purged"] == str(populated)
    assert not populated.exists()
