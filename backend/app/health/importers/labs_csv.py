"""Laboratory CSV importer: horizontal (dates as columns) → one observation
per row (H1, docs/health/DESIGN.md §5.2, ACCEPTANCE.md H1).

Accepted layout (the manual-export contract; header names may be Japanese
or English, and label columns may appear in any order before the dates):

    項目名,項目略称,単位,基準値範囲,基準値MIN,基準値MAX,6/22/2006,3/8/2008,...
    クレアチニン,Cr,mg/dL,0.60-1.10,0.60,1.10,0.9,0.8

- ``項目``/``項目名``/``metric``/``item``  — verbatim metric name (required, first)
- ``項目略称``/``略称``/``abbr``            — optional; a fallback alias key
- ``単位``/``unit``                        — optional
- ``基準値``/``基準値範囲``/``reference``   — optional; per-ROW text like ``10-30``
- ``基準値MIN``/``基準値MAX``               — optional; structured bounds that
  take precedence over parsing the range text
- every remaining column must parse as a date
  (YYYY-MM-DD, YYYY/MM/DD, or M/D/YYYY)

A reference-range change over time is represented by the SAME metric on a
second row with the new range and its values under the applicable date
columns — each observation stores the range of its own row, so ranges stay
per observation date and are never overwritten globally.

Normalization rules (H-D3, DESIGN.md health §6):
- numeric cell + unit that normalizes to the metric's canonical unit →
  value_num + unit, quality ``valid``;
- numeric cell + unknown unit → value_text keeps the raw string, value_num
  and unit stay NULL, quality ``provisional`` (no false conversion);
- qualitative cell (``<5``, ``(-)`` …) → value_text, quality ``valid``;
- blank cell → no observation (nothing is invented);
- unknown metric name → quarantine_records, never guessed into a neighbour;
- date-only precision stays date-only (time_precision='date').

Idempotency: a deterministic fingerprint over
(source_kind, metric, date, raw value, raw unit, source_name) — re-importing
an unchanged file inserts nothing; a changed cell yields exactly one new row
(the superseded row keeps its own provenance, append-only).
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from .. import catalog as catalog_mod
from .. import config, store

logger = logging.getLogger("cairn.health")

PARSER_NAME = "labs_csv"
PARSER_VERSION = "1"
SUBJECT_ID = "self"
DEFAULT_SOURCE_NAME = "lab_sheet"

_METRIC_HEADERS = {"項目", "項目名", "metric", "item"}
# A second, abbreviated name column (real Japanese lab sheets carry both a
# full name and an abbreviation, e.g. クレアチニン / Cr). Recognized so it is
# not mistaken for a date, and used as a fallback alias key.
_ABBREV_HEADERS = {"項目略称", "略称", "abbr", "abbreviation"}
_UNIT_HEADERS = {"単位", "unit"}
_REFERENCE_HEADERS = {"基準値", "基準値範囲", "reference", "reference_range"}
_REF_MIN_HEADERS = {"基準値min", "基準値下限", "ref_min", "reference_min"}
_REF_MAX_HEADERS = {"基準値max", "基準値上限", "ref_max", "reference_max"}
_RANGE_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[-−–~〜]\s*(-?\d+(?:\.\d+)?)\s*$")


class LabsCsvError(Exception):
    """Malformed input; the caller rolls back normalized writes."""


class HeaderCols:
    """Resolved column indexes from the header row."""

    __slots__ = ("abbrev", "unit", "reference", "ref_min", "ref_max", "dates")

    def __init__(self):
        self.abbrev = self.unit = self.reference = None
        self.ref_min = self.ref_max = None
        self.dates: list[tuple[int, date]] = []


def _parse_date(text: str) -> date | None:
    # ISO first, then the US M/D/Y layout real spreadsheets export.
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _parse_header(header: list[str]) -> HeaderCols:
    if not header or header[0].strip() not in _METRIC_HEADERS:
        raise LabsCsvError(
            "first column must be the metric name (項目/項目名/metric/item)")
    cols = HeaderCols()
    for idx, name in enumerate(header[1:], start=1):
        cleaned = name.strip()
        low = cleaned.lower()
        if cleaned in _ABBREV_HEADERS and cols.abbrev is None:
            cols.abbrev = idx
        elif cleaned in _UNIT_HEADERS and cols.unit is None:
            cols.unit = idx
        elif cleaned in _REFERENCE_HEADERS and cols.reference is None:
            cols.reference = idx
        elif low in _REF_MIN_HEADERS and cols.ref_min is None:
            cols.ref_min = idx
        elif low in _REF_MAX_HEADERS and cols.ref_max is None:
            cols.ref_max = idx
        else:
            parsed = _parse_date(cleaned)
            if parsed is None:
                raise LabsCsvError(
                    f"header column {idx} is neither a known label nor a date")
            cols.dates.append((idx, parsed))
    if not cols.dates:
        raise LabsCsvError("no date columns found in header")
    return cols


def _cell(cells: list[str], idx: int | None) -> str | None:
    if idx is None or idx >= len(cells):
        return None
    value = cells[idx].strip()
    return value or None


def _resolve_reference(cells: list[str], cols: HeaderCols
                       ) -> tuple[float | None, float | None, str | None]:
    """Prefer explicit 基準値MIN/MAX columns; fall back to parsing a range
    text like ``5-45``. Returns (low, high, text)."""
    text = _cell(cells, cols.reference)
    lo = _try_float(_cell(cells, cols.ref_min) or "")
    hi = _try_float(_cell(cells, cols.ref_max) or "")
    if lo is not None or hi is not None:
        return lo, hi, text
    if text:
        match = _RANGE_RE.match(text)
        if match:
            return float(match.group(1)), float(match.group(2)), text
        return None, None, text
    return None, None, None


def _try_float(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _fingerprint(original_metric: str, observed: date, original_value: str,
                 original_unit: str | None, source_name: str) -> str:
    key = "|".join([
        PARSER_NAME, original_metric, observed.isoformat(),
        original_value, original_unit or "", source_name,
    ])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _snapshot_source(home: Path, src: Path, sha256: str) -> Path:
    """Copy the source immutably into raw/ BEFORE parsing (原本第一)."""
    raw_dir = home / "raw" / PARSER_NAME
    raw_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(raw_dir, config.DIR_MODE)
    target = raw_dir / f"{sha256[:16]}_{src.name}"
    if not target.exists():
        shutil.copy2(src, target)
    config.protect_file(target)
    return target


def run(source: str | Path, *, source_name: str = DEFAULT_SOURCE_NAME,
        catalog_dir: Path | None = None, home: Path | None = None) -> dict:
    """Import one laboratory CSV. Returns redacted stats (safe to print)."""
    src = Path(source).expanduser()
    if not src.is_file():
        raise LabsCsvError(f"not a readable file: {src.name}")

    home = home or config.ensure_home()
    cat = catalog_mod.load(catalog_dir)
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    stored = _snapshot_source(home, src, digest)

    conn = store.connect(home, create=True)
    now = datetime.now(timezone.utc)

    # Source registration is keyed on the content hash: an unchanged file
    # reuses its source_files row; every attempt gets its own import_run.
    row = conn.execute(
        "SELECT id FROM source_files WHERE sha256 = ?", [digest]
    ).fetchone()
    if row:
        source_file_id = row[0]
    else:
        source_file_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO source_files (id, source_kind, original_name,"
            " stored_path, sha256, size_bytes, acquired_at, parser_name,"
            " parser_version, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [source_file_id, PARSER_NAME, src.name,
             str(stored.relative_to(home)), digest, src.stat().st_size,
             now, PARSER_NAME, PARSER_VERSION, "imported"],
        )

    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO import_runs (id, source_file_id, started_at, status)"
        " VALUES (?,?,?, 'running')",
        [run_id, source_file_id, now],
    )

    inserted = skipped = quarantined = 0
    try:
        conn.execute("BEGIN TRANSACTION")
        catalog_mod.refresh_store(conn, cat)
        with src.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            if header is None:
                raise LabsCsvError("empty file")
            cols = _parse_header(header)

            for row_idx, cells in enumerate(reader, start=2):
                if not cells or not cells[0].strip():
                    continue
                if len(cells) > len(header):
                    raise LabsCsvError(f"row {row_idx} has more cells than the header")
                original_metric = cells[0].strip()
                raw_unit = _cell(cells, cols.unit)
                ref_low, ref_high, ref_text = _resolve_reference(cells, cols)
                # Map on the full name, then fall back to the abbreviation.
                metric = cat.resolve_metric(original_metric)
                if metric is None:
                    abbrev = _cell(cells, cols.abbrev)
                    if abbrev:
                        metric = cat.resolve_metric(abbrev)

                for col_idx, observed in cols.dates:
                    raw_value = cells[col_idx].strip() if col_idx < len(cells) else ""
                    if not raw_value:
                        continue  # blank: nothing is invented
                    row_ref = f"row={row_idx},col={observed.isoformat()}"

                    if metric is None:
                        # Idempotent like observations: the same cell of the
                        # same source is quarantined once, not per re-import.
                        if conn.execute(
                            "SELECT 1 FROM quarantine_records WHERE"
                            " source_file_id=? AND reason_code='unknown_metric'"
                            " AND source_row_ref=?",
                            [source_file_id, row_ref],
                        ).fetchone():
                            skipped += 1
                            continue
                        conn.execute(
                            "INSERT INTO quarantine_records (id, source_file_id,"
                            " import_run_id, reason_code, original_metric,"
                            " original_unit, source_row_ref, payload_json,"
                            " created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            [uuid.uuid4().hex, source_file_id, run_id,
                             "unknown_metric", original_metric, raw_unit,
                             row_ref,
                             json.dumps({"value": raw_value}, ensure_ascii=False),
                             now],
                        )
                        quarantined += 1
                        continue

                    fp = _fingerprint(original_metric, observed, raw_value,
                                      raw_unit, source_name)
                    if conn.execute(
                        "SELECT 1 FROM observations WHERE fingerprint = ?", [fp]
                    ).fetchone():
                        skipped += 1
                        continue

                    numeric = _try_float(raw_value)
                    canonical_unit = cat.canonical_unit_for(raw_unit)
                    if numeric is not None and raw_unit is not None and (
                        canonical_unit is None
                        or canonical_unit != metric.canonical_unit
                    ):
                        # Unknown/incompatible unit: keep the fact, no false
                        # normalization (value_text carries the raw string).
                        value_num, value_text, unit = None, raw_value, None
                        quality = "provisional"
                    elif numeric is not None:
                        value_num, value_text = numeric, None
                        unit = metric.canonical_unit if raw_unit else None
                        quality = "valid"
                    else:
                        value_num, value_text, unit = None, raw_value, None
                        quality = "valid"

                    conn.execute(
                        "INSERT INTO observations (id, subject_id, metric_id,"
                        " original_metric, value_num, value_text, unit,"
                        " original_value, original_unit, observed_date,"
                        " time_precision, reference_low, reference_high,"
                        " reference_text, source_name, source_file_id,"
                        " source_row_ref, fingerprint, mapping_version,"
                        " quality_status) VALUES"
                        " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [uuid.uuid4().hex, SUBJECT_ID, metric.metric_id,
                         original_metric, value_num, value_text, unit,
                         raw_value, raw_unit, observed, "date",
                         ref_low, ref_high, ref_text, source_name,
                         source_file_id, row_ref, fp, cat.mapping_version,
                         quality],
                    )
                    inserted += 1
        conn.execute("COMMIT")
    except Exception as exc:
        conn.execute("ROLLBACK")
        conn.execute(
            "UPDATE import_runs SET completed_at=?, status='failed',"
            " error_code=?, error_detail_redacted=? WHERE id=?",
            [datetime.now(timezone.utc), type(exc).__name__,
             "import failed; inspect locally via run id", run_id],
        )
        conn.close()
        logger.error("labs_csv import failed run=%s error=%s", run_id,
                     type(exc).__name__)
        raise

    conn.execute(
        "UPDATE import_runs SET completed_at=?, inserted=?, skipped=?,"
        " quarantined=?, status=? WHERE id=?",
        [datetime.now(timezone.utc), inserted, skipped, quarantined,
         "ok" if quarantined == 0 else "partial", run_id],
    )
    conn.close()

    logger.info("labs_csv import run=%s inserted=%d skipped=%d quarantined=%d",
                run_id, inserted, skipped, quarantined)
    return {
        "run_id": run_id,
        "source_sha256": digest[:16],
        "inserted": inserted,
        "skipped": skipped,
        "quarantined": quarantined,
        "catalog_version": cat.catalog_version,
        "mapping_version": cat.mapping_version,
    }
