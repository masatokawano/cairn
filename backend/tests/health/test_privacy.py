"""H0/H1 privacy: log redaction, transactional rollback, repository audit.

Maps to ACCEPTANCE.md H1 (rollback preserves raw / logs carry no values)
and H0 (repository audit detects likely health artifacts).
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from app.health import audit, store
from app.health.importers import labs_csv


def test_logs_contain_no_values_metrics_or_paths(imported, catalog_dir,
                                                 labs_csv_path, caplog):
    with caplog.at_level(logging.INFO, logger="cairn.health"):
        labs_csv.run(labs_csv_path, catalog_dir=catalog_dir)
    text = caplog.text
    for leaked in ("1.23", "11", "23", "<5", "Synthetic", "Mystery",
                   str(labs_csv_path)):
        assert leaked not in text, f"log leaked {leaked!r}"
    assert "labs_csv import run=" in text     # counts and ids ARE logged


def test_malformed_input_rolls_back_but_preserves_raw(health_home, catalog_dir,
                                                      labs_csv_path, tmp_path,
                                                      caplog):
    bad = tmp_path / "bad.csv"
    # Valid first row, then a row with more cells than the header → the
    # importer fails MID-file and the partial normalized writes roll back.
    bad.write_text(
        "項目,単位,基準値,2031-02-03\n"
        "Synthetic-A,arb-U/L,10-30,11\n"
        "Synthetic-B,arb-mg/dL,0.60-1.10,1.23,EXTRA-CELL\n",
        "utf-8",
    )
    with caplog.at_level(logging.ERROR, logger="cairn.health"):
        with pytest.raises(labs_csv.LabsCsvError):
            labs_csv.run(bad, catalog_dir=catalog_dir)

    home = health_home.resolve()
    conn = store.connect(home)
    try:
        assert conn.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
        (run,) = conn.execute(
            "SELECT status, error_code FROM import_runs").fetchall()
    finally:
        conn.close()
    assert run == ("failed", "LabsCsvError")
    # The raw snapshot survives the rollback (原本第一).
    assert len(list((home / "raw" / "labs_csv").iterdir())) == 1
    assert "1.23" not in caplog.text


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_audit_detects_committable_health_artifacts(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / "export.xml").write_text("<HealthData/>")
    (repo / "notes.duckdb").write_bytes(b"")
    (repo / "血液検査結果.csv").write_text("項目名\n")   # non-ASCII pattern
    result = audit.scan(repo)
    assert result["ok"] is False
    assert set(result["untracked"]) == {"export.xml", "notes.duckdb",
                                        "血液検査結果.csv"}


def test_audit_gitignored_health_file_is_warning_not_failure(tmp_path):
    """A gitignored real-data file cannot be committed → ok stays True, but
    it is surfaced in 'ignored' so doctor can nudge relocation."""
    repo = _git_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text("scratch/\n")
    (repo / "scratch").mkdir()
    (repo / "scratch" / "export.xml").write_text("<HealthData/>")
    result = audit.scan(repo)
    assert result["ok"] is True
    assert result["untracked"] == []
    assert "scratch/export.xml" in result["ignored"]


def test_audit_clean_repo_passes(tmp_path):
    repo = _git_repo(tmp_path / "clean")
    (repo / "README.md").write_text("hello")
    assert audit.scan(repo)["ok"] is True


def test_audit_on_this_repository_has_nothing_committable():
    """Live guard (AGENTS.md invariant 9): the real Cairn worktree must never
    contain committable health artifacts. Gitignored scratch data (e.g. a
    lab CSV placed in temp/ for import) is a separate, non-blocking warning."""
    result = audit.scan()
    assert result.get("ok") is True, result
    assert result.get("tracked") == []
