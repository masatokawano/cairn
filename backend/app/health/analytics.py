"""Factual event/observation window queries (H2).

Bounded comparison only — DESIGN.md health §2: the validation loop never
claims causality automatically. These functions report what was measured
before/after an event window; interpretation belongs to H6.

Uncertainty handling: an event's start is an interval
[start_earliest, start_latest] (a single day at date precision, a month at
month precision). "Before" strictly precedes the earliest bound, "after"
strictly follows the latest; observations inside the uncertainty window are
reported separately, never silently assigned to a side.
"""
from __future__ import annotations

from datetime import timedelta


def daily_summary(conn, metric_id: str) -> list[dict]:
    """Per-day aggregate of a metric's numeric observations, regenerated from
    the normalized rows (ACCEPTANCE H3). Non-numeric/provisional rows are
    excluded from the numbers but counted."""
    rows = conn.execute(
        "SELECT observed_date, count(*) FILTER (WHERE value_num IS NOT NULL),"
        "       count(*) FILTER (WHERE value_num IS NULL),"
        "       min(value_num), max(value_num), avg(value_num),"
        "       sum(value_num)"
        " FROM observations WHERE metric_id = ? AND observed_date IS NOT NULL"
        " GROUP BY observed_date ORDER BY observed_date",
        [metric_id],
    ).fetchall()
    keys = ("date", "n", "n_excluded", "min", "max", "mean", "sum")
    out = []
    for r in rows:
        d = dict(zip(keys, r))
        if d["mean"] is not None:
            d["mean"] = round(d["mean"], 4)
        out.append(d)
    return out


def weekly_summary(conn, metric_id: str) -> list[dict]:
    """Per-ISO-week aggregate, regenerated from the normalized rows."""
    rows = conn.execute(
        "SELECT strftime(observed_date, '%G-W%V') AS wk,"
        "       count(*) FILTER (WHERE value_num IS NOT NULL),"
        "       min(value_num), max(value_num), avg(value_num), sum(value_num)"
        " FROM observations WHERE metric_id = ? AND observed_date IS NOT NULL"
        " GROUP BY wk ORDER BY wk",
        [metric_id],
    ).fetchall()
    keys = ("week", "n", "min", "max", "mean", "sum")
    out = []
    for r in rows:
        d = dict(zip(keys, r))
        if d["mean"] is not None:
            d["mean"] = round(d["mean"], 4)
        out.append(d)
    return out


def broken_references(conn, home) -> list[dict]:
    """Rows whose provenance no longer resolves on disk (H4).

    Detects two failure modes: a source_files row whose raw snapshot is
    missing, and a document whose ``extracted_text_path`` is missing. Reports
    ids and kinds only — no clinical content. ``home`` is the data home root
    the stored paths are relative to.
    """
    from pathlib import Path

    home = Path(home)
    broken: list[dict] = []
    for sid, kind, stored in conn.execute(
        "SELECT id, source_kind, stored_path FROM source_files"
    ).fetchall():
        if not (home / stored).exists():
            n_obs = conn.execute(
                "SELECT count(*) FROM observations WHERE source_file_id=?", [sid]
            ).fetchone()[0]
            broken.append({"kind": "missing_source_snapshot",
                           "source_file_id": sid, "source_kind": kind,
                           "observations_affected": n_obs})
    for did, path in conn.execute(
        "SELECT id, extracted_text_path FROM documents"
        " WHERE extracted_text_path IS NOT NULL"
    ).fetchall():
        if not (home / path).exists():
            broken.append({"kind": "missing_extracted_text",
                           "document_id": did})
    return broken


def documents(conn) -> list[dict]:
    """Registered documents with metadata only (no extracted text body)."""
    rows = conn.execute(
        "SELECT id, document_kind, title, document_date, issuer,"
        "       extraction_status FROM documents"
        " ORDER BY document_date NULLS LAST, id"
    ).fetchall()
    keys = ("id", "kind", "title", "date", "issuer", "extraction_status")
    return [dict(zip(keys, r)) for r in rows]


def data_quality(conn) -> list[dict]:
    """Per-metric coverage/quality: counts, date span, provisional share
    (ACCEPTANCE H3 data-quality report). Counts only — no values."""
    rows = conn.execute(
        "SELECT metric_id,"
        "       count(*),"
        "       count(*) FILTER (WHERE quality_status='provisional'),"
        "       count(DISTINCT source_name),"
        "       min(observed_date), max(observed_date)"
        " FROM observations GROUP BY metric_id ORDER BY metric_id"
    ).fetchall()
    keys = ("metric_id", "n", "provisional", "sources", "first", "last")
    return [dict(zip(keys, r)) for r in rows]


def current_events(conn) -> list[dict]:
    """Events not superseded by another entry (append-only chain heads)."""
    rows = conn.execute(
        "SELECT e.id, e.kind, e.label, e.start_raw, e.start_earliest,"
        "       e.start_latest, e.end_raw, e.end_latest, e.time_precision,"
        "       e.status, e.dose_value, e.dose_unit, e.confidence,"
        "       e.source_file_id"
        " FROM events e"
        " WHERE NOT EXISTS (SELECT 1 FROM events s WHERE s.supersedes_id = e.id)"
        " ORDER BY e.start_earliest NULLS LAST, e.id"
    ).fetchall()
    keys = ("id", "kind", "label", "start_raw", "start_earliest",
            "start_latest", "end_raw", "end_latest", "time_precision",
            "status", "dose_value", "dose_unit", "confidence",
            "source_file_id")
    return [dict(zip(keys, r)) for r in rows]


def overlay(conn) -> list[dict]:
    """Observation timeline with the current events active on each date.

    An event is active on date d when start_earliest <= d and
    (end_latest is NULL or d <= end_latest). Events without a start are
    never overlaid (their uncertainty stays visible in current_events).
    """
    events = [e for e in current_events(conn) if e["start_earliest"]]
    rows = conn.execute(
        "SELECT observed_date, metric_id, original_value, quality_status"
        " FROM observations ORDER BY observed_date, metric_id, fingerprint"
    ).fetchall()
    out = []
    for observed, metric_id, value, quality in rows:
        active = [e["id"] for e in events
                  if e["start_earliest"] <= observed
                  and (e["end_latest"] is None or observed <= e["end_latest"])]
        out.append({"observed_date": observed, "metric_id": metric_id,
                    "value": value, "quality_status": quality,
                    "active_events": active})
    return out


def event_response(conn, event_id: str, *, window_days: int = 90,
                   metrics: list[str] | None = None) -> dict:
    """Factual before/after summary around one event's start window."""
    event = next((e for e in current_events(conn) if e["id"] == event_id), None)
    if event is None:
        raise KeyError(f"unknown or superseded event: {event_id!r}")
    if not event["start_earliest"]:
        return {"event": event, "window_days": window_days,
                "note": "event start is unknown — no window comparison possible",
                "metrics": {}}

    lo = event["start_earliest"] - timedelta(days=window_days)
    hi = event["start_latest"] + timedelta(days=window_days)
    metric_sql = ""
    params: list = [lo, hi]
    if metrics:
        metric_sql = f" AND metric_id IN ({','.join('?' * len(metrics))})"
        params.extend(metrics)
    rows = conn.execute(
        "SELECT metric_id, observed_date, value_num, original_value, unit,"
        "       quality_status FROM observations"
        " WHERE observed_date BETWEEN ? AND ?"
        + metric_sql +
        " ORDER BY metric_id, observed_date, fingerprint",
        params,
    ).fetchall()

    metrics: dict[str, dict] = {}
    for metric_id, observed, value_num, original, unit, quality in rows:
        bucket = metrics.setdefault(metric_id, {
            "unit": unit, "before": [], "after": [],
            "in_start_window": [], "non_numeric": 0,
        })
        if value_num is None:
            bucket["non_numeric"] += 1
            continue
        point = {"date": observed.isoformat(), "value": value_num,
                 "original": original}
        if observed < event["start_earliest"]:
            bucket["before"].append(point)
        elif observed > event["start_latest"]:
            bucket["after"].append(point)
        else:
            bucket["in_start_window"].append(point)

    for bucket in metrics.values():
        for side in ("before", "after"):
            values = [p["value"] for p in bucket[side]]
            bucket[f"{side}_summary"] = {
                "n": len(values),
                "min": min(values) if values else None,
                "max": max(values) if values else None,
                "mean": round(sum(values) / len(values), 4) if values else None,
            }
    return {"event": event, "window_days": window_days, "metrics": metrics}


def event_response_summary(conn, event_id: str, *, metrics: list[str],
                           window_days: int = 90) -> dict:
    """SQL-aggregated event comparison for bounded disclosure surfaces.

    Unlike ``event_response`` (used by factual reports that need points), this
    never materializes high-frequency raw rows in Python.
    """
    event = next((e for e in current_events(conn) if e["id"] == event_id), None)
    if event is None:
        raise KeyError(f"unknown or superseded event: {event_id!r}")
    if not event["start_earliest"]:
        return {"event": event, "window_days": window_days,
                "note": "event start is unknown — no window comparison possible",
                "metrics": {}}

    lo = event["start_earliest"] - timedelta(days=window_days)
    hi = event["start_latest"] + timedelta(days=window_days)
    placeholders = ",".join("?" * len(metrics))
    rows = conn.execute(
        "SELECT metric_id, max(unit),"
        " count(value_num) FILTER (WHERE observed_date < ?),"
        " min(value_num) FILTER (WHERE observed_date < ?),"
        " max(value_num) FILTER (WHERE observed_date < ?),"
        " avg(value_num) FILTER (WHERE observed_date < ?),"
        " count(value_num) FILTER (WHERE observed_date > ?),"
        " min(value_num) FILTER (WHERE observed_date > ?),"
        " max(value_num) FILTER (WHERE observed_date > ?),"
        " avg(value_num) FILTER (WHERE observed_date > ?),"
        " count(value_num) FILTER (WHERE observed_date BETWEEN ? AND ?),"
        " count(*) FILTER (WHERE value_num IS NULL)"
        " FROM observations WHERE observed_date BETWEEN ? AND ?"
        f" AND metric_id IN ({placeholders}) GROUP BY metric_id"
        " ORDER BY metric_id",
        [event["start_earliest"]] * 4 + [event["start_latest"]] * 4
        + [event["start_earliest"], event["start_latest"], lo, hi, *metrics],
    ).fetchall()
    out = {}
    for row in rows:
        out[row[0]] = {
            "unit": row[1],
            "before_summary": {"n": row[2], "min": row[3], "max": row[4],
                               "mean": round(row[5], 4) if row[5] is not None else None},
            "after_summary": {"n": row[6], "min": row[7], "max": row[8],
                              "mean": round(row[9], 4) if row[9] is not None else None},
            "in_start_window_count": row[10],
            "non_numeric": row[11],
        }
    return {"event": event, "window_days": window_days, "metrics": out}
