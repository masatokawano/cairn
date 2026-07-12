"""Apple Health export importer (H3, docs/health/DESIGN.md §5.1).

Adds high-frequency personal measurements WITHOUT turning Cairn into a raw
sensor archive: only an allowlist of HealthKit types enters the store, and
those land in the same ``observations`` table as lab values (H-D1 keeps it
out of cairn.db regardless).

Design points (ACCEPTANCE H3):

- **streaming** — ``export.xml`` is parsed with ``iterparse`` and the root
  is cleared after every record, so a multi-hundred-MB export never loads
  fully into memory. Input may be the ``export.zip`` (the member
  ``.../export.xml`` is streamed straight out of the archive) or a bare
  ``export.xml``.
- **allowlist only** — see ``_ALLOWED`` (8 record types for the 7 metrics:
  steps, resting HR, HRV, body mass, sleep, systolic+diastolic BP, exercise
  time). Every other type is counted and dropped; its values are never read
  or logged. ``Workout``/``WorkoutRoute`` elements (location-bearing) are
  skipped entirely.
- **deterministic dedup** — Apple exports duplicate records across sources
  (iPhone + Watch, overlapping syncs). A fingerprint over
  (type, source, start, end, raw value, unit) collapses exact duplicates and
  makes re-import idempotent.
- **instant vs interval** — a record with start == end is an instant; a
  span (sleep, exercise time) keeps both bounds and ``time_precision`` says
  which. Original timestamps and their UTC offsets are preserved.
- **transactional** — an interrupted parse rolls back the normalized writes;
  the raw snapshot in ``raw/`` survives.
"""
from __future__ import annotations

import csv
import hashlib
import logging
import os
import shutil
import uuid
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from .. import catalog as catalog_mod
from .. import config, store

logger = logging.getLogger("cairn.health")

PARSER_NAME = "apple_health"
PARSER_VERSION = "1"
SUBJECT_ID = "self"

# DuckDB is columnar: parameterized INSERT (execute/executemany) is ~700
# rows/s — hopeless for a multi-hundred-thousand-record Apple Health export.
# The native fast path is COPY FROM CSV (~140k rows/s), so rows are streamed
# to a temporary CSV inside the protected data home (PRIVACY.md §3 permits
# temp files there) and bulk-loaded in one COPY. Memory stays flat: rows go
# to disk as they parse, only the dedup fingerprint set is held.
_COLUMNS = (
    "id", "subject_id", "metric_id", "original_metric", "value_num",
    "value_text", "unit", "original_value", "original_unit", "observed_start",
    "observed_end", "observed_date", "time_precision", "source_name",
    "device_name", "source_file_id", "source_row_ref", "fingerprint",
    "mapping_version", "quality_status",
)


def _csv_field(v) -> str:
    """Format a value for the COPY CSV. None -> '' (loaded as NULL via
    NULLSTR ''); datetimes/dates -> ISO 8601; everything else -> str."""
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)

# Sleep is a category type; the rest are quantities. All eight are mapped to
# metrics via healthkit_identifier in metrics.yml.
SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"


class AppleHealthError(Exception):
    """Malformed export; the caller rolls back normalized writes."""


def _parse_dt(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None


def _fingerprint(hk_type: str, source: str, start: str, end: str,
                 value: str, unit: str) -> str:
    key = "|".join([PARSER_NAME, hk_type, source, start, end, value, unit])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _snapshot_source(home: Path, src: Path, sha256: str) -> Path:
    raw_dir = home / "raw" / PARSER_NAME
    raw_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(raw_dir, config.DIR_MODE)
    target = raw_dir / f"{sha256[:16]}_{src.name}"
    if not target.exists():
        shutil.copy2(src, target)
    config.protect_file(target)
    return target


def _open_xml_stream(src: Path):
    """Yield a binary stream of export.xml from a .zip or a bare .xml.

    For a zip, the member is streamed without extracting the whole archive.
    """
    if zipfile.is_zipfile(src):
        zf = zipfile.ZipFile(src)
        member = next((n for n in zf.namelist()
                       if n.endswith("export.xml")
                       and not n.endswith("export_cda.xml")), None)
        if member is None:
            zf.close()
            raise AppleHealthError("no export.xml inside the Apple Health zip")
        return zf.open(member), zf
    return src.open("rb"), None


def _sha256_file(src: Path) -> str:
    h = hashlib.sha256()
    with src.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def run(source: str | Path, *, catalog_dir: Path | None = None,
        home: Path | None = None) -> dict:
    """Import one Apple Health export. Returns redacted stats."""
    src = Path(source).expanduser()
    if not src.is_file():
        raise AppleHealthError(f"not a readable file: {src.name}")

    home = home or config.ensure_home()
    cat = catalog_mod.load(catalog_dir)
    allowed = cat.healthkit                       # HK id -> metric_id
    digest = _sha256_file(src)
    stored = _snapshot_source(home, src, digest)

    conn = store.connect(home, create=True)
    now = datetime.now(timezone.utc)

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

    inserted = skipped = 0
    ignored_types: dict[str, int] = {}
    stream, zf = _open_xml_stream(src)
    # Temp CSV staging file inside the protected home (0600). Rows stream here
    # during parse; one COPY bulk-loads them. Deleted in every exit path.
    tmp_csv = home / "backups" / f".apple-staging-{run_id}.csv"
    try:
        # Seen fingerprints already in the store for this source (idempotent
        # re-import) plus those seen in THIS pass (within-export duplicates).
        seen: set[str] = {r[0] for r in conn.execute(
            "SELECT fingerprint FROM observations WHERE source_file_id=?",
            [source_file_id],
        ).fetchall()}

        fd = os.open(tmp_csv, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as csv_fh:
            writer = csv.writer(csv_fh)
            context = iter(ET.iterparse(stream, events=("start", "end")))
            _, root = next(context)
            for event, elem in context:
                if event != "end" or elem.tag != "Record":
                    continue
                hk_type = elem.get("type") or ""
                metric_id = allowed.get(hk_type)
                if metric_id is None:
                    ignored_types[hk_type] = ignored_types.get(hk_type, 0) + 1
                    root.clear()
                    continue
                metric = cat.metrics[metric_id]

                raw_value = (elem.get("value") or "").strip()
                raw_unit = (elem.get("unit") or "").strip() or None
                source_name = (elem.get("sourceName") or "unknown").strip()
                device = elem.get("device")
                start_s = elem.get("startDate") or ""
                end_s = elem.get("endDate") or start_s
                if not raw_value or not start_s:
                    root.clear()
                    continue

                fp = _fingerprint(hk_type, source_name, start_s, end_s,
                                  raw_value, raw_unit or "")
                if fp in seen:
                    skipped += 1
                    root.clear()
                    continue
                seen.add(fp)

                start_dt = _parse_dt(start_s)
                end_dt = _parse_dt(end_s)
                observed_date = start_dt.date() if start_dt else None
                precision = "instant" if start_dt == end_dt else "interval"

                if hk_type == SLEEP_TYPE:
                    # Category record: derive duration (min); keep the sleep-
                    # phase category as the original value.
                    minutes = None
                    if start_dt and end_dt:
                        minutes = round((end_dt - start_dt).total_seconds() / 60, 3)
                    value_num, value_text = minutes, raw_value
                    unit = "min" if minutes is not None else None
                    quality = "valid" if minutes is not None else "provisional"
                    precision = "interval"
                else:
                    numeric = _try_float(raw_value)
                    canonical_unit = cat.canonical_unit_for(raw_unit)
                    if numeric is not None and (
                            canonical_unit is None
                            or canonical_unit != metric.canonical_unit):
                        value_num, value_text, unit = None, raw_value, None
                        quality = "provisional"
                    elif numeric is not None:
                        value_num, value_text, unit = numeric, None, metric.canonical_unit
                        quality = "valid"
                    else:
                        value_num, value_text, unit = None, raw_value, None
                        quality = "valid"

                writer.writerow([_csv_field(v) for v in (
                    uuid.uuid4().hex, SUBJECT_ID, metric_id, hk_type,
                    value_num, value_text, unit, raw_value, raw_unit,
                    start_dt, end_dt, observed_date, precision, source_name,
                    device, source_file_id, None, fp, cat.mapping_version,
                    quality)])
                inserted += 1
                root.clear()

        conn.execute("BEGIN TRANSACTION")
        if inserted:
            conn.execute(
                f"COPY observations ({','.join(_COLUMNS)}) FROM ? "
                "(FORMAT CSV, HEADER false, NULLSTR '', QUOTE '\"', ESCAPE '\"')",
                [str(tmp_csv)],
            )
        conn.execute("COMMIT")
    except Exception as exc:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        conn.execute(
            "UPDATE import_runs SET completed_at=?, status='failed',"
            " error_code=?, error_detail_redacted=? WHERE id=?",
            [datetime.now(timezone.utc), type(exc).__name__,
             "apple health import failed; inspect locally via run id", run_id],
        )
        conn.close()
        logger.error("apple_health import failed run=%s error=%s", run_id,
                     type(exc).__name__)
        raise
    finally:
        stream.close()
        if zf is not None:
            zf.close()
        try:
            tmp_csv.unlink()
        except FileNotFoundError:
            pass

    ignored_count = sum(ignored_types.values())
    conn.execute(
        "UPDATE import_runs SET completed_at=?, inserted=?, skipped=?,"
        " status='ok' WHERE id=?",
        [datetime.now(timezone.utc), inserted, skipped, run_id],
    )
    conn.close()

    logger.info("apple_health import run=%s inserted=%d skipped=%d "
                "ignored_types=%d ignored_records=%d", run_id, inserted,
                skipped, len(ignored_types), ignored_count)
    return {
        "run_id": run_id,
        "source_sha256": digest[:16],
        "inserted": inserted,
        "skipped": skipped,
        "ignored_type_count": len(ignored_types),
        "ignored_record_count": ignored_count,
        "mapping_version": cat.mapping_version,
    }


def _try_float(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None
