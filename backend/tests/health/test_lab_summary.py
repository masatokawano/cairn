"""H1 factual report: deterministic bytes, factual-only vocabulary,
protected output location."""
from __future__ import annotations

import stat
from datetime import datetime, timezone

from app.health import store
from app.health.reports import lab_summary

FIXED_NOW = datetime(2031, 9, 1, 12, 0, 0, tzinfo=timezone.utc)

# The factual report must never editorialize (H0_H1_TASK prohibited content).
FORBIDDEN = ("diagnos", "recommend", "risk", " safe", "dangerous",
             "正常", "異常", "危険", "診断", "リスク", "推奨")


def test_report_is_deterministic(imported):
    home, _ = imported
    conn = store.connect(home.resolve())
    try:
        md1, hash1 = lab_summary.build(conn, now=FIXED_NOW)
        md2, hash2 = lab_summary.build(conn, now=FIXED_NOW)
    finally:
        conn.close()
    assert md1 == md2
    assert hash1 == hash2


def test_report_factual_content(imported):
    home, _ = imported
    conn = store.connect(home.resolve())
    try:
        md, _ = lab_summary.build(conn, now=FIXED_NOW)
    finally:
        conn.close()
    assert "generated_by: cairn/health.lab_summary/t1" in md
    # Latest value per metric, exactly as recorded.
    assert "| synthetic_a | 2031-08-19 | 23 |" in md
    # Missingness is visible, not papered over.
    assert "synthetic_a: 2031-05-11" in md
    # Quarantine surfaced as counts.
    assert "unknown_metric: 3" in md
    # Source snapshot identifiers present.
    assert "synthetic_labs.csv" in md and "sha256=" in md


def test_report_has_no_interpretive_vocabulary(imported):
    home, _ = imported
    conn = store.connect(home.resolve())
    try:
        md, _ = lab_summary.build(conn, now=FIXED_NOW)
    finally:
        conn.close()
    lowered = md.lower()
    for word in FORBIDDEN:
        assert word.lower() not in lowered, f"interpretive vocabulary: {word!r}"


def test_write_places_report_in_protected_home(imported):
    home, _ = imported
    out = lab_summary.write(home.resolve(), now=FIXED_NOW)
    assert out["path"] == "reports/lab-summary.md"
    path = home.resolve() / out["path"]
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
