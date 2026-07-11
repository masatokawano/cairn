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


def test_audit_detects_health_artifacts(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / "export.xml").write_text("<HealthData/>")
    (repo / "notes.duckdb").write_bytes(b"")
    result = audit.scan(repo)
    assert result["ok"] is False
    hits = set(result["untracked"])
    assert hits == {"export.xml", "notes.duckdb"}


def test_audit_clean_repo_passes(tmp_path):
    repo = _git_repo(tmp_path / "clean")
    (repo / "README.md").write_text("hello")
    assert audit.scan(repo)["ok"] is True


def test_audit_on_this_repository_is_clean():
    """Live guard: the actual Cairn worktree must never contain health
    artifacts (AGENTS.md invariant 9). Fails the suite if one appears."""
    result = audit.scan()
    assert result.get("ok") is True, result
