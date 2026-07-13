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
        # Make synthetic_d a genuine single-measurement metric.
        conn.execute(
            "DELETE FROM observations WHERE metric_id='synthetic_d'"
            " AND observed_date > DATE '2031-02-03'")
        md = vault_reports.build_lab_trends(conn, now=FIXED_NOW)
    finally:
        conn.close()
    # The lone value appears ONLY in the singles section, never as a table.
    assert "## 単発の測定（1回のみ・経過ではない）" in md
    singles_part = md.split("## 単発の測定")[1]
    assert "synthetic_d" in singles_part
    assert "### synthetic_d" not in md          # no per-metric history table


def test_hostile_free_text_cannot_break_markdown(health_home, catalog_dir,
                                                 tmp_path, monkeypatch):
    """Codex review finding: free text from the store (source names, event
    labels, verbatim values) must be escaped — a `|`, newline, or leading
    `#` must not break tables or spoof structure (invariant 4)."""
    from app.health.importers import labs_csv

    # Hostile VALUE under the normal lab source (lands in trends + status).
    hostile_val = tmp_path / "hostile_val.csv"
    hostile_val.write_text(
        "項目,単位,基準値,2031-02-03,2031-03-01\n"
        'Synthetic-A,arb-U/L,10-30,"11 | 偽セル","12 | x"\n',
        "utf-8",
    )
    labs_csv.run(hostile_val, catalog_dir=catalog_dir)
    # Hostile SOURCE NAME (lands in status via the latest-value table).
    hostile_src = tmp_path / "hostile_src.csv"
    hostile_src.write_text(
        "項目,単位,基準値,2031-04-01\nSynthetic-B,arb-mg/dL,0.60-1.10,1.11\n",
        "utf-8",
    )
    labs_csv.run(hostile_src, catalog_dir=catalog_dir,
                 source_name="evil|source\n# 見出し偽装")

    conn = store.connect(health_home.resolve())
    try:
        status = vault_reports.build_current_status(conn, now=FIXED_NOW)
        trends = vault_reports.build_lab_trends(conn, now=FIXED_NOW)
    finally:
        conn.close()
    for md in (status, trends):
        for line in md.splitlines():
            assert not line.startswith("# 見出し偽装")   # 行頭見出し偽装なし
        assert "11 | 偽セル" not in md            # raw pipe would split the cell
        assert "evil|source" not in md            # raw pipe in source name
    assert "11 \\| 偽セル" in trends              # escaped form present instead
    assert "evil\\|source" in status              # newline collapsed + escaped


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
