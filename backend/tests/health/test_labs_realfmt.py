"""H1 real-world layout: abbreviation column, 基準値MIN/MAX, M/D/YYYY dates.

Driven by the actual lab-sheet format found during A-1 real-data
verification (header 項目名,項目略称,単位,基準値範囲,基準値MIN,基準値MAX,<M/D/YYYY dates>).
Fixture is fully synthetic (metric names/values invented).
"""
from __future__ import annotations

from datetime import date

from app.health import store
from app.health.importers import labs_csv

from .conftest import FIXTURES

REALFMT = FIXTURES / "synthetic_labs_realfmt.csv"


def _rows(home, sql, params=None):
    conn = store.connect(home.resolve())
    try:
        return conn.execute(sql, params or []).fetchall()
    finally:
        conn.close()


def test_realfmt_imports_via_full_name_and_abbrev(health_home, catalog_dir):
    stats = labs_csv.run(REALFMT, catalog_dir=catalog_dir)
    # A: 2 values, B: 3 values = 5; Mystery-X: 3 cells quarantined.
    assert stats["inserted"] == 5
    assert stats["quarantined"] == 3


def test_realfmt_mdy_dates_parsed(health_home, catalog_dir):
    labs_csv.run(REALFMT, catalog_dir=catalog_dir)
    dates = [r[0] for r in _rows(health_home,
        "SELECT DISTINCT observed_date FROM observations ORDER BY observed_date")]
    assert dates == [date(2031, 3, 8), date(2031, 4, 15), date(2031, 6, 22)]


def test_realfmt_structured_min_max_reference(health_home, catalog_dir):
    labs_csv.run(REALFMT, catalog_dir=catalog_dir)
    (row,) = _rows(health_home,
        "SELECT reference_low, reference_high, reference_text FROM observations"
        " WHERE metric_id='synthetic_b' AND observed_date=DATE '2031-06-22'")
    assert row == (0.60, 1.10, "0.60-1.10")


def test_realfmt_maps_by_abbreviation_when_fullname_differs(health_home,
                                                            catalog_dir):
    """The full name 合成項目A is not an alias; the abbreviation Synthetic-A
    is — mapping must succeed via the abbreviation column."""
    labs_csv.run(REALFMT, catalog_dir=catalog_dir)
    (row,) = _rows(health_home,
        "SELECT metric_id, original_metric FROM observations"
        " WHERE metric_id='synthetic_a' AND observed_date=DATE '2031-06-22'")
    assert row == ("synthetic_a", "合成項目A")


def test_realfmt_unknown_metric_still_quarantined(health_home, catalog_dir):
    labs_csv.run(REALFMT, catalog_dir=catalog_dir)
    q = _rows(health_home,
        "SELECT DISTINCT original_metric FROM quarantine_records")
    assert q == [("謎項目X",)]
