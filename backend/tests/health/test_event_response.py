"""H2 overlay + before/after windows + factual event report.

Uses the labs fixture (dates 2031-02-03 / 05-11 / 08-19) with the events
fixture (med start 2031-04-01, trip ~05-01..05-20): with a ±90d window the
02-03 observations fall before, 05-11 after, 08-19 outside.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.health import analytics, store
from app.health.importers import events_yaml
from app.health.reports import event_response

from .conftest import FIXTURES

FIXED_NOW = datetime(2031, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
FORBIDDEN = ("diagnos", "recommend", "risk", " safe", "dangerous",
             "正常", "異常", "危険", "診断", "リスク", "推奨")


def _with_events(imported):
    home, _ = imported
    events_yaml.run(FIXTURES / "synthetic_events.yml")
    return home


def test_overlay_marks_active_events(imported):
    home = _with_events(imported)
    conn = store.connect(home.resolve())
    try:
        rows = analytics.overlay(conn)
    finally:
        conn.close()
    by_date = {}
    for r in rows:
        by_date.setdefault(str(r["observed_date"]), set()).update(r["active_events"])
    # Before any event started: nothing active.
    assert by_date["2031-02-03"] == set()
    # 05-11: med active since 04-01, smoking stop since March, trip 05-01..05-20.
    assert by_date["2031-05-11"] == {"evt-med-001", "evt-smoke-001", "evt-trip-001"}
    # 08-19: trip over; open-ended events still active.
    assert by_date["2031-08-19"] == {"evt-med-001", "evt-smoke-001"}


def test_event_response_windows(imported):
    home = _with_events(imported)
    conn = store.connect(home.resolve())
    try:
        data = analytics.event_response(conn, "evt-med-001", window_days=90)
    finally:
        conn.close()
    a = data["metrics"]["synthetic_a"]
    assert [p["date"] for p in a["before"]] == ["2031-02-03"]
    assert a["after"] == []                      # 08-19 is outside +90d
    assert a["before_summary"]["n"] == 1
    assert a["before_summary"]["mean"] == 11.0
    b = data["metrics"]["synthetic_b"]
    assert [p["date"] for p in b["before"]] == ["2031-02-03"]
    assert [p["date"] for p in b["after"]] == ["2031-05-11"]


def test_event_response_month_precision_uncertainty(imported):
    """A month-precision start (smoking stop, 2031-03) keeps in-window
    observations out of both sides — nothing is silently assigned."""
    home = _with_events(imported)
    conn = store.connect(home.resolve())
    try:
        data = analytics.event_response(conn, "evt-smoke-001", window_days=90)
    finally:
        conn.close()
    b = data["metrics"]["synthetic_b"]
    assert [p["date"] for p in b["before"]] == ["2031-02-03"]
    assert [p["date"] for p in b["after"]] == ["2031-05-11"]
    assert b["in_start_window"] == []            # no obs inside March


def test_event_response_unknown_start(imported):
    home = _with_events(imported)
    conn = store.connect(home.resolve())
    try:
        data = analytics.event_response(conn, "evt-ill-001")
    finally:
        conn.close()
    assert "no window comparison possible" in data["note"]


def test_report_deterministic_and_factual(imported):
    home = _with_events(imported)
    conn = store.connect(home.resolve())
    try:
        md1, h1 = event_response.build(conn, "evt-med-001", now=FIXED_NOW)
        md2, h2 = event_response.build(conn, "evt-med-001", now=FIXED_NOW)
    finally:
        conn.close()
    assert md1 == md2 and h1 == h2
    assert "generated_by: cairn/health.event_response/t1" in md1
    assert "| synthetic_b |" in md1
    lowered = md1.lower()
    for word in FORBIDDEN:
        assert word.lower() not in lowered, f"interpretive vocabulary: {word!r}"


def test_report_never_renders_free_text_notes(imported):
    """ACCEPTANCE H2: free-text notes are not promoted into factual output."""
    home = _with_events(imported)
    out = event_response.write("evt-ill-001", home=home.resolve(), now=FIXED_NOW)
    md = (home.resolve() / out["path"]).read_text("utf-8")
    assert "synthetic-note-text" not in md
    assert "must never be interpreted" not in md


def test_report_written_into_protected_home(imported):
    import stat

    home = _with_events(imported)
    out = event_response.write("evt-med-001", home=home.resolve(), now=FIXED_NOW)
    assert out["path"] == "reports/event-response-evt-med-001.md"
    path = home.resolve() / out["path"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
