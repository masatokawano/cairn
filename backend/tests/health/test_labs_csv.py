"""H1 importer: horizontal→long, original/normalized separation, per-date
reference ranges, qualitative values, quarantine, unknown units, provenance.

Fixture expectations (tests/health/fixtures/synthetic_labs.csv):
  Synthetic-A 2 obs / Synthetic-B 3 (range change across two rows) /
  Synthetic-C 3 (two qualitative) / Synthetic-D 2 (unknown unit) = 10
  observations; Mystery-X 3 cells quarantined.
"""
from __future__ import annotations

from datetime import date

from app.health import store


def _rows(home, sql, params=None):
    conn = store.connect(home.resolve())
    try:
        return conn.execute(sql, params or []).fetchall()
    finally:
        conn.close()


def test_import_counts(imported):
    _home, stats = imported
    assert stats["inserted"] == 10
    assert stats["quarantined"] == 3
    assert stats["skipped"] == 0
    assert stats["catalog_version"] == "test-1"
    assert stats["mapping_version"] == "test-1"


def test_horizontal_to_long(imported):
    home, _ = imported
    rows = _rows(home,
        "SELECT observed_date, value_num FROM observations"
        " WHERE metric_id='synthetic_a' ORDER BY observed_date")
    assert [(r[0], r[1]) for r in rows] == [
        (date(2031, 2, 3), 11.0), (date(2031, 8, 19), 23.0)]


def test_original_and_normalized_separated(imported):
    home, _ = imported
    (row,) = _rows(home,
        "SELECT original_metric, original_value, original_unit, metric_id,"
        " value_num, unit, mapping_version, quality_status FROM observations"
        " WHERE metric_id='synthetic_b' AND observed_date=DATE '2031-02-03'")
    assert row[0] == "Synthetic-B"
    assert row[1] == "1.23"          # original preserved verbatim
    assert row[2] == "arb-mg/dL"
    assert row[3] == "synthetic_b"   # canonical, separate from original
    assert row[4] == 1.23
    assert row[5] == "arb-mg/dL"
    assert row[6] == "test-1"
    assert row[7] == "valid"


def test_date_only_precision_stays_date_only(imported):
    home, _ = imported
    rows = _rows(home,
        "SELECT time_precision, observed_start, observed_end FROM observations")
    assert all(r == ("date", None, None) for r in rows)


def test_reference_range_retained_per_date(imported):
    home, _ = imported
    rows = _rows(home,
        "SELECT observed_date, reference_low, reference_high, reference_text"
        " FROM observations WHERE metric_id='synthetic_b' ORDER BY observed_date")
    assert rows[0][1:] == (0.60, 1.10, "0.60-1.10")   # 2031-02-03
    assert rows[1][1:] == (0.60, 1.10, "0.60-1.10")   # 2031-05-11
    assert rows[2][1:] == (0.65, 1.15, "0.65-1.15")   # 2031-08-19 (changed)


def test_qualitative_value(imported):
    home, _ = imported
    (row,) = _rows(home,
        "SELECT value_num, value_text, quality_status FROM observations"
        " WHERE metric_id='synthetic_c' AND observed_date=DATE '2031-02-03'")
    assert row == (None, "<5", "valid")


def test_unknown_metric_quarantined_not_guessed(imported):
    home, _ = imported
    quarantined = _rows(home,
        "SELECT reason_code, original_metric FROM quarantine_records")
    assert len(quarantined) == 3
    assert all(r == ("unknown_metric", "Mystery-X") for r in quarantined)
    assert _rows(home,
        "SELECT count(*) FROM observations WHERE original_metric='Mystery-X'"
    )[0][0] == 0


def test_unknown_unit_preserves_original_without_false_normalization(imported):
    home, _ = imported
    rows = _rows(home,
        "SELECT value_num, value_text, unit, original_unit, quality_status"
        " FROM observations WHERE metric_id='synthetic_d' ORDER BY observed_date")
    assert rows == [
        (None, "1.5", None, "wat-units", "provisional"),
        (None, "1.7", None, "wat-units", "provisional"),
    ]


def test_every_observation_has_provenance(imported):
    home, _ = imported
    rows = _rows(home,
        "SELECT source_file_id, source_row_ref, fingerprint FROM observations")
    assert len(rows) == 10
    assert all(r[0] and r[1] and r[2] for r in rows)


def test_raw_snapshot_written_and_source_registered(imported):
    home, stats = imported
    raw = list((home.resolve() / "raw" / "labs_csv").iterdir())
    assert len(raw) == 1
    assert raw[0].name.startswith(stats["source_sha256"])
    (src,) = _rows(home,
        "SELECT original_name, status, stored_path FROM source_files")
    assert src[0] == "synthetic_labs.csv"
    assert src[1] == "imported"
    assert src[2].startswith("raw/labs_csv/")
