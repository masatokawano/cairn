"""Intervention/context event importer: YAML ledger → events table (H2).

Entry format (a YAML list; the file lives in the protected data home or
anywhere local — a snapshot is taken into raw/ either way):

    - id: evt-med-001              # author-assigned, stable, unique
      kind: medication_start
      label: Example medication
      start: 2031-04-01            # or '2031-03' (month) or '~2031-04-01'
      end: null                    # optional
      dose: {value: 10, unit: mg/day}
      route: oral                  # optional
      frequency: daily             # optional
      source: self_report          # self_report / clinician / document
      confidence: confirmed        # confirmed / estimated / uncertain
      notes: free text             # optional; NEVER auto-interpreted
      supersedes: evt-med-000      # optional correction pointer

Time semantics (ACCEPTANCE H2: no invented timestamps):

- ``YYYY-MM-DD``   → precision=date, earliest == latest
- ``YYYY-MM``      → precision=month, earliest=1st, latest=last day; the
  verbatim string is kept in *_raw and is what reports display
- ``~<date|month>``→ precision=approximate (bounds from the stripped value)
- missing start    → precision=unknown, status=uncertain — visible, not
  papered over

Append-only: an entry id, once imported, is immutable. Re-importing the
identical entry is a no-op; the same id with DIFFERENT content is refused
(``EventsError``) — corrections are new entries with ``supersedes``.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import logging
import os
import shutil
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from .. import config, store

logger = logging.getLogger("cairn.health")

PARSER_NAME = "events_yaml"
PARSER_VERSION = "1"

KINDS = frozenset({
    "medication_start", "medication_stop", "dose_change",
    "supplement_start", "supplement_stop",
    "smoking_stop", "alcohol_change", "exercise_change",
    "illness", "procedure", "travel", "context_change",
})
SOURCE_TYPES = frozenset({"self_report", "clinician", "document"})
CONFIDENCES = frozenset({"confirmed", "estimated", "uncertain"})


class EventsError(Exception):
    """Invalid ledger content. Messages reference entry ids/indexes only —
    never labels, notes or doses (they may reach logs)."""


def _parse_when(raw, where: str) -> tuple[str | None, date | None, date | None, str]:
    """→ (raw_text, earliest, latest, precision).

    `where` locates the failing field for error messages (e.g. "entry 'x'
    start"). The raw value is NEVER put in the exception — a malformed date
    field can carry arbitrary free text (PRIVACY.md §5: no values in errors)."""
    if raw is None:
        return None, None, None, "unknown"
    text = raw.isoformat() if isinstance(raw, date) else str(raw).strip()
    core, precision_hint = (text[1:].strip(), "approximate") \
        if text.startswith("~") else (text, None)
    try:
        parsed = datetime.strptime(core, "%Y-%m-%d").date()
        return text, parsed, parsed, precision_hint or "date"
    except ValueError:
        pass
    try:
        month_start = datetime.strptime(core, "%Y-%m").date()
        last = calendar.monthrange(month_start.year, month_start.month)[1]
        return (text, month_start, month_start.replace(day=last),
                precision_hint or "month")
    except ValueError:
        raise EventsError(f"{where}: unparseable date"
                          f" (expected YYYY-MM-DD, YYYY-MM, or ~prefix)")


def _entry_hash(entry: dict) -> str:
    canonical = json.dumps(entry, ensure_ascii=False, sort_keys=True,
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate(index: int, entry) -> dict:
    if not isinstance(entry, dict):
        raise EventsError(f"entry #{index} is not a mapping")
    entry_id = entry.get("id")
    if not entry_id or not isinstance(entry_id, str):
        raise EventsError(f"entry #{index} is missing a string 'id'")
    kind = entry.get("kind")
    if kind not in KINDS:
        raise EventsError(f"entry {entry_id!r}: unknown kind (allowed: "
                          f"{', '.join(sorted(KINDS))})")
    source_type = entry.get("source", "self_report")
    if source_type not in SOURCE_TYPES:
        raise EventsError(f"entry {entry_id!r}: invalid source")
    confidence = entry.get("confidence", "uncertain")
    if confidence not in CONFIDENCES:
        raise EventsError(f"entry {entry_id!r}: invalid confidence")
    dose = entry.get("dose")
    if dose is not None:
        if (not isinstance(dose, dict) or "value" not in dose
                or "unit" not in dose):
            raise EventsError(f"entry {entry_id!r}: dose must be "
                              "{{value, unit}}")
        try:
            float(dose["value"])
        except (TypeError, ValueError):
            raise EventsError(f"entry {entry_id!r}: dose.value must be numeric")
    return {"id": entry_id, "kind": kind, "source_type": source_type,
            "confidence": confidence, "dose": dose}


def _snapshot_source(home: Path, src: Path, sha256: str) -> Path:
    raw_dir = home / "raw" / PARSER_NAME
    raw_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(raw_dir, config.DIR_MODE)
    target = raw_dir / f"{sha256[:16]}_{src.name}"
    if not target.exists():
        shutil.copy2(src, target)
    config.protect_file(target)
    return target


def run(source: str | Path, *, home: Path | None = None) -> dict:
    """Import an event ledger file. Returns redacted stats."""
    import yaml

    src = Path(source).expanduser()
    if not src.is_file():
        raise EventsError(f"not a readable file: {src.name}")

    home = home or config.ensure_home()
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    stored = _snapshot_source(home, src, digest)

    entries = yaml.safe_load(src.read_text("utf-8"))
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise EventsError("ledger must be a YAML list of entries")

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
            [source_file_id, "events", src.name,
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
    try:
        conn.execute("BEGIN TRANSACTION")
        known_ids = {r[0] for r in
                     conn.execute("SELECT id FROM events").fetchall()}
        batch_ids: set[str] = set()

        for index, entry in enumerate(entries, start=1):
            checked = _validate(index, entry)
            entry_id = checked["id"]
            if entry_id in batch_ids:
                raise EventsError(f"duplicate id in ledger: {entry_id!r}")
            batch_ids.add(entry_id)
            digest_entry = _entry_hash(entry)

            existing = conn.execute(
                "SELECT entry_hash FROM events WHERE id = ?", [entry_id]
            ).fetchone()
            if existing:
                if existing[0] != digest_entry:
                    raise EventsError(
                        f"entry {entry_id!r} already imported with different"
                        " content — events are append-only; add a new entry"
                        " with 'supersedes' instead of editing"
                    )
                skipped += 1
                continue

            supersedes = entry.get("supersedes")
            if supersedes and supersedes not in known_ids | batch_ids:
                raise EventsError(
                    f"entry {entry_id!r} supersedes unknown id {supersedes!r}"
                )

            start_raw, start_lo, start_hi, precision = _parse_when(
                entry.get("start"), f"entry {entry_id!r} start")
            end_raw, end_lo, end_hi, _ = _parse_when(
                entry.get("end"), f"entry {entry_id!r} end") \
                if entry.get("end") is not None else (None, None, None, None)

            if start_raw is None:
                status = "uncertain"
            elif end_raw is not None:
                status = "completed"
            else:
                status = entry.get("status", "active")
                if status not in ("active", "completed", "uncertain"):
                    raise EventsError(f"entry {entry_id!r}: invalid status")

            dose = checked["dose"]
            conn.execute(
                "INSERT INTO events (id, kind, label, start_raw,"
                " start_earliest, start_latest, end_raw, end_earliest,"
                " end_latest, time_precision, status, dose_value, dose_unit,"
                " route, frequency, source_type, source_file_id, confidence,"
                " notes, supersedes_id, entry_hash, imported_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [entry_id, checked["kind"], entry.get("label"), start_raw,
                 start_lo, start_hi, end_raw, end_lo, end_hi, precision,
                 status,
                 float(dose["value"]) if dose else None,
                 dose["unit"] if dose else None,
                 entry.get("route"), entry.get("frequency"),
                 checked["source_type"], source_file_id,
                 checked["confidence"], entry.get("notes"),
                 supersedes, digest_entry, now],
            )
            inserted += 1
        conn.execute("COMMIT")
    except Exception as exc:
        conn.execute("ROLLBACK")
        conn.execute(
            "UPDATE import_runs SET completed_at=?, status='failed',"
            " error_code=?, error_detail_redacted=? WHERE id=?",
            [datetime.now(timezone.utc), type(exc).__name__,
             "events import failed; inspect locally via run id", run_id],
        )
        conn.close()
        logger.error("events import failed run=%s error=%s", run_id,
                     type(exc).__name__)
        raise

    conn.execute(
        "UPDATE import_runs SET completed_at=?, inserted=?, skipped=?,"
        " status='ok' WHERE id=?",
        [datetime.now(timezone.utc), inserted, skipped, run_id],
    )
    conn.close()
    logger.info("events import run=%s inserted=%d skipped=%d",
                run_id, inserted, skipped)
    return {"run_id": run_id, "source_sha256": digest[:16],
            "inserted": inserted, "skipped": skipped}
