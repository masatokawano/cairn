"""H1 idempotency: unchanged re-import inserts nothing; a changed cell
affects exactly one record (append-only, both versions traceable)."""
from __future__ import annotations

from app.health import store
from app.health.importers import labs_csv


def _count(home, table="observations"):
    conn = store.connect(home.resolve())
    try:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_reimport_unchanged_is_idempotent(imported, catalog_dir, labs_csv_path):
    home, first = imported
    again = labs_csv.run(labs_csv_path, catalog_dir=catalog_dir)
    assert first["inserted"] == 10
    assert again["inserted"] == 0
    assert again["quarantined"] == 0            # quarantine is idempotent too
    assert again["skipped"] == 13               # 10 obs + 3 quarantined cells
    assert _count(home) == 10
    assert _count(home, "quarantine_records") == 3
    assert _count(home, "source_files") == 1    # same sha256 → same source row


def test_changed_cell_affects_only_that_record(imported, catalog_dir,
                                               labs_csv_path, tmp_path):
    home, _ = imported
    changed = tmp_path / "changed.csv"
    changed.write_text(
        labs_csv_path.read_text("utf-8").replace(",11,,23", ",12,,23"),
        "utf-8",
    )
    stats = labs_csv.run(changed, catalog_dir=catalog_dir)
    assert stats["inserted"] == 1               # only the changed cell
    assert stats["skipped"] == 9                # the 9 unchanged observations
    # A different file is a different source: its unknown-metric cells are
    # quarantined again with provenance to the NEW source file.
    assert stats["quarantined"] == 3
    assert _count(home) == 11                   # append-only: 12 coexists with 11
    assert _count(home, "quarantine_records") == 6

    conn = store.connect(home.resolve())
    try:
        rows = conn.execute(
            "SELECT original_value, source_file_id FROM observations"
            " WHERE metric_id='synthetic_a'"
            " AND observed_date=DATE '2031-02-03' ORDER BY original_value"
        ).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == ["11", "12"]
    assert rows[0][1] != rows[1][1]             # each traceable to its own source


def test_fingerprint_is_deterministic():
    from datetime import date

    a = labs_csv._fingerprint("Synthetic-A", date(2031, 2, 3), "11", "arb-U/L", "lab_sheet")
    b = labs_csv._fingerprint("Synthetic-A", date(2031, 2, 3), "11", "arb-U/L", "lab_sheet")
    c = labs_csv._fingerprint("Synthetic-A", date(2031, 2, 3), "12", "arb-U/L", "lab_sheet")
    assert a == b != c
