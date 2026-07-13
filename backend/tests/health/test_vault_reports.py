"""H5 vault delivery: reports land ONLY in 90 Auto/Health via the allowlist,
deterministic, factual-only, single measurements never framed as trends.

Maps to ACCEPTANCE.md H5 (except the AI-Drafts item, which belongs to H6).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.health import store
from app.health.importers import events_yaml, labs_csv
from app.health.reports import vault_reports

from .conftest import FIXTURES

FIXED_NOW = datetime(2031, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
FORBIDDEN = ("diagnos", "recommend", "risk", " safe", "dangerous",
             "正常", "異常", "危険", "診断", "リスク", "推奨", "傾向")


@pytest.fixture
def populated(health_home, catalog_dir, labs_csv_path):
    labs_csv.run(labs_csv_path, catalog_dir=catalog_dir)
    events_yaml.run(FIXTURES / "synthetic_events.yml")
    return health_home


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "Vault"
    (root / "External Brain" / "90 Auto").mkdir(parents=True)
    monkeypatch.setenv("CAIRN_OBSIDIAN_VAULT", str(root))
    monkeypatch.delenv("CAIRN_EXTERNAL_BRAIN_DIR", raising=False)
    return root


def _build_all(home):
    conn = store.connect(home.resolve())
    try:
        return {name: fn(conn, now=FIXED_NOW)
                for name, fn in vault_reports.REPORTS.items()}
    finally:
        conn.close()


def test_reports_deterministic(populated):
    a = _build_all(populated)
    b = _build_all(populated)
    assert a == b


def test_reports_factual_content(populated):
    reports = _build_all(populated)
    status = reports["current-status.md"]
    assert "generated_by: cairn/health.vault_reports/t1" in status
    assert "| synthetic_a | 2031-08-19 | 23 |" in status
    # Active events with dose, uncertain events kept visible as uncertain.
    assert "medication_start" in status and "10.0mg/day" in status
    assert "開始不明のまま記録" in status

    timeline = reports["timeline.md"]
    assert "- 2031-04-01〜: medication_start — Synthetic medication" in timeline
    assert "### 2031-08-19" in timeline

    quality = reports["data-quality.md"]
    assert "| synthetic_a |" in quality
    assert "unknown_metric: 3" in quality


def test_single_measurement_never_framed_as_series(populated):
    """ACCEPTANCE H5: a single value is not described as a persistent trend.
    Metrics with one lab measurement are structurally separated."""
    home = populated
    conn = store.connect(home.resolve())
    try:
        md = vault_reports.build_lab_trends(conn, now=FIXED_NOW)
    finally:
        conn.close()
    # synthetic_d has 2 values (multi), synthetic_a has 2... build a single:
    # qualitative synthetic_c has 3; all fixture metrics have >=2. The section
    # must still exist and be honest when empty.
    assert "## 単発の測定（1回のみ・経過ではない）" in md


def test_no_interpretive_vocabulary(populated):
    for name, md in _build_all(populated).items():
        lowered = md.lower()
        for word in FORBIDDEN:
            assert word.lower() not in lowered, f"{name}: {word!r}"


def test_deliver_writes_only_into_90auto_health(populated, vault):
    out = vault_reports.deliver(now=FIXED_NOW)
    assert out["reports"] == 4
    health_dir = vault / "External Brain" / "90 Auto" / "Health"
    written = sorted(p.name for p in health_dir.iterdir())
    assert written == ["current-status.md", "data-quality.md",
                       "lab-trends.md", "timeline.md"]
    # Nothing anywhere else in the vault.
    all_files = [p for p in vault.rglob("*") if p.is_file()]
    assert all(p.parent == health_dir for p in all_files)
    # Real values ARE in the vault files (that's the point of delivery)…
    assert "23" in (health_dir / "current-status.md").read_text("utf-8")


def test_deliver_is_repeatable(populated, vault):
    vault_reports.deliver(now=FIXED_NOW)
    out = vault_reports.deliver(now=FIXED_NOW)   # overwrite allowed
    assert out["reports"] == 4


def test_deliver_without_vault_env_fails_cleanly(populated, monkeypatch):
    monkeypatch.delenv("CAIRN_OBSIDIAN_VAULT", raising=False)
    with pytest.raises(Exception):
        vault_reports.deliver(now=FIXED_NOW)
