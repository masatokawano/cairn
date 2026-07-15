"""H2 event ledger: schema validation, uncertainty-aware dates, append-only
supersession, idempotent re-import. Maps to ACCEPTANCE.md H2."""
from __future__ import annotations

from datetime import date

import pytest

from app.health import store
from app.health.importers import events_yaml

from .conftest import FIXTURES

EVENTS = FIXTURES / "synthetic_events.yml"


def _rows(home, sql, params=None):
    conn = store.connect(home.resolve())
    try:
        return conn.execute(sql, params or []).fetchall()
    finally:
        conn.close()


@pytest.fixture
def events_imported(health_home):
    stats = events_yaml.run(EVENTS)
    return health_home, stats


def test_import_counts_and_registration(events_imported):
    home, stats = events_imported
    assert stats["inserted"] == 4
    assert stats["skipped"] == 0
    (src,) = _rows(home, "SELECT source_kind, status FROM source_files")
    assert src == ("events", "imported")


def test_date_precisions_no_invented_timestamps(events_imported):
    home, _ = events_imported
    rows = {r[0]: r[1:] for r in _rows(home,
        "SELECT id, start_raw, start_earliest, start_latest, time_precision,"
        " status FROM events")}
    # Exact date: earliest == latest.
    assert rows["evt-med-001"] == (
        "2031-04-01", date(2031, 4, 1), date(2031, 4, 1), "date", "active")
    # Month precision: verbatim string kept, bounds = calendar month.
    assert rows["evt-smoke-001"] == (
        "2031-03", date(2031, 3, 1), date(2031, 3, 31), "month", "active")
    # Approximate: ~ prefix preserved, precision marked.
    assert rows["evt-trip-001"][0] == "~2031-05-01"
    assert rows["evt-trip-001"][3] == "approximate"
    assert rows["evt-trip-001"][4] == "completed"      # has an end
    # Missing start: visible as uncertainty, nothing fabricated.
    assert rows["evt-ill-001"] == (None, None, None, "unknown", "uncertain")


def test_dose_optional_but_structured(events_imported):
    home, _ = events_imported
    rows = {r[0]: (r[1], r[2]) for r in _rows(home,
        "SELECT id, dose_value, dose_unit FROM events")}
    assert rows["evt-med-001"] == (10.0, "mg/day")
    assert rows["evt-smoke-001"] == (None, None)


def test_reimport_is_idempotent(events_imported):
    home, _ = events_imported
    again = events_yaml.run(EVENTS)
    assert again["inserted"] == 0
    assert again["skipped"] == 4
    assert _rows(home, "SELECT count(*) FROM events")[0][0] == 4


def test_editing_an_imported_entry_is_refused(events_imported, tmp_path):
    home, _ = events_imported
    tampered = tmp_path / "tampered.yml"
    tampered.write_text(
        EVENTS.read_text("utf-8").replace("value: 10", "value: 99"), "utf-8")
    with pytest.raises(events_yaml.EventsError, match="append-only"):
        events_yaml.run(tampered)
    # The refused batch rolled back entirely.
    assert _rows(home, "SELECT count(*) FROM events")[0][0] == 4


def test_correction_via_supersession(events_imported, tmp_path):
    home, _ = events_imported
    fixed = tmp_path / "fixed.yml"
    fixed.write_text(
        EVENTS.read_text("utf-8") + "\n"
        "- id: evt-med-002\n"
        "  kind: dose_change\n"
        "  label: Synthetic medication\n"
        "  start: 2031-07-01\n"
        "  dose: {value: 20, unit: mg/day}\n"
        "  source: self_report\n"
        "  confidence: confirmed\n"
        "  supersedes: evt-med-001\n",
        "utf-8",
    )
    stats = events_yaml.run(fixed)
    assert stats["inserted"] == 1
    assert stats["skipped"] == 4
    # Old row untouched (append-only); chain recorded.
    (row,) = _rows(home,
        "SELECT supersedes_id FROM events WHERE id='evt-med-002'")
    assert row[0] == "evt-med-001"

    from app.health import analytics
    conn = store.connect(home.resolve())
    try:
        current = {e["id"] for e in analytics.current_events(conn)}
    finally:
        conn.close()
    assert "evt-med-002" in current
    assert "evt-med-001" not in current       # superseded → not current


def test_unknown_kind_and_bad_supersedes_rejected(health_home, tmp_path):
    bad_kind = tmp_path / "bad_kind.yml"
    bad_kind.write_text(
        "- id: evt-x\n  kind: teleportation\n  start: 2031-01-01\n", "utf-8")
    with pytest.raises(events_yaml.EventsError, match="unknown kind"):
        events_yaml.run(bad_kind)

    bad_ref = tmp_path / "bad_ref.yml"
    bad_ref.write_text(
        "- id: evt-y\n  kind: illness\n  supersedes: evt-nope\n", "utf-8")
    with pytest.raises(events_yaml.EventsError, match="unknown id"):
        events_yaml.run(bad_ref)


def test_validation_errors_do_not_leak_content(health_home, tmp_path, caplog):
    import logging

    bad = tmp_path / "bad.yml"
    bad.write_text(
        "- id: evt-z\n  kind: illness\n  start: not-a-date\n"
        "  notes: SECRET-NOTE\n", "utf-8")
    with caplog.at_level(logging.ERROR, logger="cairn.health"):
        with pytest.raises(events_yaml.EventsError):
            events_yaml.run(bad)
    assert "SECRET-NOTE" not in caplog.text


def test_unparseable_date_error_omits_raw_value(health_home, tmp_path):
    """R5 (2026-07-15 review): a malformed date field can carry arbitrary free
    text; the EventsError message must name the entry+field but never the raw
    value (PRIVACY.md §5). The CLI surfaces this message verbatim."""
    bad = tmp_path / "bad_date.yml"
    # The whole 'start' value is a malformed date carrying a sensitive token.
    bad.write_text(
        "- id: evt-sensitive\n  kind: medication\n"
        "  start: 2031-04-01-LEAKY-CONTEXT-TOKEN\n", "utf-8")
    with pytest.raises(events_yaml.EventsError) as ei:
        events_yaml.run(bad)
    msg = str(ei.value)
    assert "LEAKY-CONTEXT-TOKEN" not in msg  # raw value must not leak
    assert "evt-sensitive" in msg  # but the entry id (safe) locates it
    assert "start" in msg
