"""H3 Apple Health import: allowlist enforcement, dedup, instant/interval,
sleep duration, ignored-type counting, zip streaming, aggregates.

Maps to ACCEPTANCE.md H3. Fixture is fully synthetic.
"""
from __future__ import annotations

import zipfile
from datetime import date

import pytest

from app.health import store
from app.health.importers import apple_health

from .conftest import FIXTURES

EXPORT_XML = FIXTURES / "synthetic_apple_export.xml"


def _rows(home, sql, params=None):
    conn = store.connect(home.resolve())
    try:
        return conn.execute(sql, params or []).fetchall()
    finally:
        conn.close()


@pytest.fixture
def ah_imported(health_home):
    stats = apple_health.run(EXPORT_XML)
    return health_home, stats


def test_allowlist_only_and_dedup(ah_imported):
    home, stats = ah_imported
    # 12 allowlisted records: 1 exact duplicate skipped, 1 epoch-1970
    # sentinel quarantined, 10 inserted.
    assert stats["inserted"] == 10
    assert stats["skipped"] == 1
    assert stats["quarantined"] == 1
    # Two non-allowlisted types are counted, values never read.
    assert stats["ignored_type_count"] == 2
    assert stats["ignored_record_count"] == 2
    metrics = {r[0] for r in _rows(home,
        "SELECT DISTINCT metric_id FROM observations")}
    assert metrics == {"step_count", "resting_heart_rate",
                       "heart_rate_variability", "body_mass",
                       "blood_pressure_systolic", "blood_pressure_diastolic",
                       "exercise_time", "sleep_analysis"}


def test_ignored_types_never_enter_store(ah_imported):
    home, _ = ah_imported
    assert _rows(home,
        "SELECT count(*) FROM observations WHERE original_metric LIKE '%Caffeine%'"
        " OR original_metric LIKE '%AudioExposure%'")[0][0] == 0


def test_workout_and_route_excluded(ah_imported):
    home, _ = ah_imported
    # No workout/route data of any kind is stored.
    assert _rows(home,
        "SELECT count(*) FROM observations WHERE original_metric LIKE '%Workout%'"
    )[0][0] == 0


def test_instant_vs_interval_preserved(ah_imported):
    home, _ = ah_imported
    (hr,) = _rows(home,
        "SELECT time_precision FROM observations"
        " WHERE metric_id='resting_heart_rate'")
    assert hr[0] == "instant"
    (ex,) = _rows(home,
        "SELECT time_precision, observed_start, observed_end FROM observations"
        " WHERE metric_id='exercise_time'")
    assert ex[0] == "interval"
    assert ex[1] is not None and ex[2] is not None and ex[1] != ex[2]


def test_source_and_dates_preserved(ah_imported):
    home, _ = ah_imported
    rows = _rows(home,
        "SELECT source_name, observed_date FROM observations"
        " WHERE metric_id='step_count' ORDER BY observed_date")
    assert rows[0] == ("iPhone", date(2031, 6, 1))
    assert rows[1] == ("Apple Watch", date(2031, 6, 2))


def test_bp_value_normalized(ah_imported):
    home, _ = ah_imported
    rows = {r[0]: (r[1], r[2], r[3]) for r in _rows(home,
        "SELECT metric_id, value_num, unit, quality_status FROM observations"
        " WHERE metric_id LIKE 'blood_pressure%'")}
    assert rows["blood_pressure_systolic"] == (118.0, "mmHg", "valid")
    assert rows["blood_pressure_diastolic"] == (76.0, "mmHg", "valid")


def test_unit_mismatch_is_provisional(ah_imported):
    home, _ = ah_imported
    rows = _rows(home,
        "SELECT original_unit, value_num, value_text, quality_status"
        " FROM observations WHERE metric_id='body_mass' ORDER BY observed_date")
    # kg → valid, normalized; lb → provisional, original preserved, no convert.
    assert rows[0] == ("kg", 70.5, None, "valid")
    assert rows[1] == ("lb", None, "155.2", "provisional")


def test_sleep_duration_derived(ah_imported):
    home, _ = ah_imported
    (row,) = _rows(home,
        "SELECT value_num, value_text, unit, time_precision FROM observations"
        " WHERE metric_id='sleep_analysis'")
    # 23:00 → 01:00 next day = 120 minutes; category kept as original value.
    assert row[0] == 120.0
    assert row[1] == "HKCategoryValueSleepAnalysisAsleepCore"
    assert row[2] == "min"
    assert row[3] == "interval"


def test_reimport_is_idempotent(ah_imported):
    home, _ = ah_imported
    again = apple_health.run(EXPORT_XML)
    assert again["inserted"] == 0
    assert again["quarantined"] == 0     # sentinel already seen, not re-quarantined
    assert again["skipped"] == 12        # all 12 allowlisted records seen
    assert _rows(home, "SELECT count(*) FROM observations")[0][0] == 10


def test_sentinel_epoch_date_quarantined_not_inserted(ah_imported):
    home, _ = ah_imported
    # The 1970 step record is quarantined, so no observation carries a 1970
    # date and the step timeline stays clean.
    assert _rows(home,
        "SELECT count(*) FROM observations WHERE observed_date < DATE '2000-01-01'"
    )[0][0] == 0
    q = _rows(home,
        "SELECT reason_code, original_metric FROM quarantine_records")
    assert q == [("sentinel_date", "HKQuantityTypeIdentifierStepCount")]


def test_import_from_zip_stream(health_home, tmp_path):
    zpath = tmp_path / "export.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.write(EXPORT_XML, "apple_health_export/export.xml")
    stats = apple_health.run(zpath)
    assert stats["inserted"] == 10


def test_daily_and_weekly_aggregates_regenerate(ah_imported):
    from app.health import analytics

    home, _ = ah_imported
    conn = store.connect(home.resolve())
    try:
        daily = analytics.daily_summary(conn, "step_count")
        weekly = analytics.weekly_summary(conn, "step_count")
    finally:
        conn.close()
    assert [(d["date"], d["sum"]) for d in daily] == [
        (date(2031, 6, 1), 1200.0), (date(2031, 6, 2), 800.0)]
    # 06-01 is Sunday (ISO 2031-W22), 06-02 Monday (W23) — distinct weeks.
    assert [(w["week"], w["sum"]) for w in weekly] == [
        ("2031-W22", 1200.0), ("2031-W23", 800.0)]


def test_logs_carry_no_values(health_home, caplog):
    import logging
    import re

    with caplog.at_level(logging.INFO, logger="cairn.health"):
        apple_health.run(EXPORT_XML)
    # Distinctive value/category tokens must never appear (short ints like
    # 95/118 are skipped: they collide with hex run-ids and counts).
    for leaked in ("70.5", "155.2", "AsleepCore", "Caffeine", "AudioExposure"):
        assert leaked not in caplog.text, f"log leaked {leaked!r}"
    # The one INFO line is a counts-only summary in a fixed format.
    line = [m for m in caplog.messages if "apple_health import run=" in m]
    assert len(line) == 1
    assert re.fullmatch(
        r"apple_health import run=[0-9a-f]{32} inserted=\d+ skipped=\d+ "
        r"quarantined=\d+ ignored_types=\d+ ignored_records=\d+", line[0])
