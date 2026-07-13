"""Factual health reports delivered to the Obsidian vault (H5, ADR-0005).

Four reproducible reports land in ``90 Auto/Health/`` (allowlist category
"health", overwrite allowed — regenerated like the other 90 Auto lists):

- ``current-status.md``  latest value per metric, with quality caveats
- ``timeline.md``        lab observations and events on one axis, plus
                         monthly aggregates for high-frequency metrics
- ``lab-trends.md``      per-metric value history (facts only; metrics with
                         a single measurement are listed separately and
                         never presented as a trend)
- ``data-quality.md``    coverage/provisional/quarantine counts

Discipline (same as the store-side reports):
- values exactly as recorded; sections separate source facts from derived
  calculations; zero interpretive vocabulary (enforced by test);
- deterministic for a fixed store state and ``now``;
- interpretive drafts are NOT written here — they belong to H6 and go to
  ``00 Inbox/AI Drafts`` (new-only), never into 90 Auto.

Privacy: these files contain real measurements. The folder is excluded from
every vault sync mechanism by default (PRIVACY.md §10 decision H5-P1) and is
not indexed by the Obsidian connector (90 Auto is outside the indexed set).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

# Free text from the health store (source names from Apple Health devices,
# event labels, verbatim lab values) is untrusted for markdown purposes
# (AGENTS.md invariant 4): _esc collapses newlines and escapes inline
# metacharacters including `|`, so a hostile string can neither break a
# table nor spoof structure (every emit position is prefixed, so a leading
# '#' can never reach line start either).
from ...deliver.auto_lists import _esc
from .. import analytics, config, store

TEMPLATE_VERSION = "1"
GENERATED_BY = f"cairn/health.vault_reports/t{TEMPLATE_VERSION}"

# Metrics whose per-sample volume makes raw listing useless in a report —
# they appear as monthly aggregates instead (facts derived by count/sum/mean,
# labelled as derived).
HIGH_FREQUENCY = ("step_count", "sleep_analysis", "exercise_time")


def _header(title: str, now: datetime, result_hash: str) -> list[str]:
    return [
        f"# {title}",
        "",
        f"- generated_by: {GENERATED_BY}",
        f"- generated_at: {now.isoformat()}",
        f"- result_hash: {result_hash}",
        "- 本ファイルは自動生成（上書きされます）。値は記録されたままの事実で、"
        "解釈は含みません。",
        "",
    ]


def _hash(rows) -> str:
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


def build_current_status(conn, *, now: datetime) -> str:
    latest = conn.execute(
        "SELECT o.metric_id, o.observed_date, o.original_value,"
        "       o.original_unit, o.reference_text, o.quality_status, o.source_name"
        " FROM observations o"
        " JOIN (SELECT metric_id, max(observed_date) AS d FROM observations"
        "       WHERE metric_id IS NOT NULL GROUP BY metric_id) m"
        "   ON o.metric_id = m.metric_id AND o.observed_date = m.d"
        " ORDER BY o.metric_id, o.fingerprint"
    ).fetchall()
    events = analytics.current_events(conn)
    result_hash = _hash((latest, [(e["id"], e["status"]) for e in events]))

    lines = _header("Health — current status (factual)", now, result_hash)
    lines += ["## 最新値（記録どおり・source facts）", "",
              "| metric | date | value | unit | reference | quality | source |",
              "|---|---|---|---|---|---|---|"]
    for metric_id, d, value, unit, ref, quality, source_name in latest:
        lines.append(f"| {metric_id} | {d.isoformat()} | {_esc(value)} |"
                     f" {_esc(unit)} | {_esc(ref)} | {quality} | {_esc(source_name)} |")
    lines += ["", "## 進行中のイベント", ""]
    active = [e for e in events if e["status"] == "active"]
    if active:
        for e in active:
            dose = (f" {e['dose_value']}{_esc(e['dose_unit'])}"
                    if e["dose_value"] is not None else "")
            lines.append(f"- {e['kind']}: {_esc(e['label'] or e['id'])}{dose}"
                         f"（{_esc(e['start_raw'])}〜, {e['confidence']}）")
    else:
        lines.append("- なし（またはイベント台帳が未入力）")
    uncertain = [e for e in events if e["status"] == "uncertain"]
    if uncertain:
        lines += ["", "## 開始時期が不確実なイベント", ""]
        for e in uncertain:
            lines.append(f"- {e['kind']}: {_esc(e['label'] or e['id'])}（開始不明のまま記録）")
    lines.append("")
    return "\n".join(lines)


def build_timeline(conn, *, now: datetime, lab_dates: int = 8,
                   months: int = 12) -> str:
    events = analytics.current_events(conn)
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT observed_date FROM observations"
        " WHERE source_name = 'lab_sheet'"
        " ORDER BY observed_date DESC LIMIT ?",
        [lab_dates],
    ).fetchall()]
    lab_rows = conn.execute(
        "SELECT observed_date, metric_id, original_value, original_unit"
        " FROM observations WHERE observed_date IN"
        f" ({','.join('?' * len(dates)) or 'NULL'})"
        " AND source_name = 'lab_sheet'"
        " ORDER BY observed_date DESC, metric_id",
        list(dates),
    ).fetchall() if dates else []
    monthly = {}
    for metric_id in HIGH_FREQUENCY:
        monthly[metric_id] = conn.execute(
            "SELECT strftime(observed_date, '%Y-%m') AS m,"
            "       count(*) FILTER (WHERE value_num IS NOT NULL),"
            "       round(avg(value_num), 1), round(sum(value_num), 0)"
            " FROM observations WHERE metric_id = ?"
            " GROUP BY m ORDER BY m DESC LIMIT ?",
            [metric_id, months],
        ).fetchall()
    result_hash = _hash((lab_rows, monthly,
                         [(e["id"], e["start_raw"]) for e in events]))

    lines = _header("Health — timeline (factual)", now, result_hash)
    lines += ["## イベント（介入・文脈）", ""]
    if events:
        for e in events:
            span = f"{_esc(e['start_raw']) or '開始不明'}" + (
                f" 〜 {_esc(e['end_raw'])}" if e["end_raw"] else "〜")
            lines.append(f"- {span}: {e['kind']} — {_esc(e['label'] or e['id'])}")
    else:
        lines.append("- 未入力（events.yml を編集して import してください）")
    lines += ["", f"## 検査値（直近 {len(dates)} 検査日・source facts）", ""]
    current_date = None
    for d, metric_id, value, unit in lab_rows:
        if d != current_date:
            lines += [f"### {d.isoformat()}", ""]
            current_date = d
        lines.append(f"- {metric_id}: {_esc(value)} {_esc(unit)}")
    lines += ["", f"## 高頻度指標の月次集計（直近 {months} ヶ月・derived）", ""]
    for metric_id, rows in monthly.items():
        if not rows:
            continue
        lines += [f"### {metric_id}", "",
                  "| month | n | mean | sum |", "|---|---|---|---|"]
        for m, n, mean, total in rows:
            lines.append(f"| {m} | {n} | {mean} | {total} |")
        lines.append("")
    return "\n".join(lines)


def build_lab_trends(conn, *, now: datetime, per_metric: int = 10) -> str:
    metrics = [r[0] for r in conn.execute(
        "SELECT DISTINCT metric_id FROM observations"
        " WHERE source_name = 'lab_sheet' ORDER BY metric_id").fetchall()]
    history: dict[str, list] = {}
    for metric_id in metrics:
        history[metric_id] = conn.execute(
            "SELECT observed_date, original_value, original_unit,"
            "       reference_text FROM observations WHERE metric_id = ?"
            " AND source_name = 'lab_sheet'"
            " ORDER BY observed_date DESC LIMIT ?",
            [metric_id, per_metric],
        ).fetchall()
    result_hash = _hash(history)

    lines = _header("Health — lab value history (factual)", now, result_hash)
    singles = {m: h for m, h in history.items() if len(h) == 1}
    multi = {m: h for m, h in history.items() if len(h) >= 2}
    lines += [f"## 経過（各指標 直近 {per_metric} 回まで・source facts）", ""]
    for metric_id, rows in multi.items():
        lines += [f"### {metric_id}", "",
                  "| date | value | unit | reference |", "|---|---|---|---|"]
        for d, value, unit, ref in rows:
            lines.append(f"| {d.isoformat()} | {_esc(value)} | {_esc(unit)} | {_esc(ref)} |")
        lines.append("")
    # A single measurement is a point, not a series — kept apart so the
    # layout itself cannot suggest a course over time.
    lines += ["## 単発の測定（1回のみ・経過ではない）", ""]
    if singles:
        for metric_id, ((d, value, unit, ref),) in singles.items():
            lines.append(f"- {metric_id}: {d.isoformat()} {_esc(value)} {_esc(unit)}")
    else:
        lines.append("- なし")
    lines.append("")
    return "\n".join(lines)


def build_data_quality(conn, *, now: datetime) -> str:
    quality = analytics.data_quality(conn)
    quarantine = conn.execute(
        "SELECT reason_code, count(*) FROM quarantine_records"
        " GROUP BY reason_code ORDER BY reason_code").fetchall()
    result_hash = _hash((quality, quarantine))

    lines = _header("Health — data quality (counts only)", now, result_hash)
    lines += ["| metric | n | provisional | sources | first | last |",
              "|---|---|---|---|---|---|"]
    for m in quality:
        lines.append(f"| {m['metric_id']} | {m['n']} | {m['provisional']} |"
                     f" {m['sources']} | {m['first']} | {m['last']} |")
    lines += ["", "## Quarantine", ""]
    if quarantine:
        for reason, count in quarantine:
            lines.append(f"- {reason}: {count}")
    else:
        lines.append("- empty")
    lines.append("")
    return "\n".join(lines)


REPORTS = {
    "current-status.md": build_current_status,
    "timeline.md": build_timeline,
    "lab-trends.md": build_lab_trends,
    "data-quality.md": build_data_quality,
}


def deliver(*, now: datetime | None = None) -> dict:
    """Generate all vault reports and write them via the allowlisted
    "health" category. Returns written paths (values stay in the files)."""
    from ...deliver import obsidian_writer

    now = now or datetime.now(timezone.utc)
    home = config.resolve_home()
    conn = store.connect(home)
    try:
        rendered = {name: fn(conn, now=now) for name, fn in REPORTS.items()}
    finally:
        conn.close()
    written = [str(obsidian_writer.write("health", name, content))
               for name, content in rendered.items()]
    return {"written": written, "reports": len(written)}
