"""Factual before/after report for one event (H2).

Same discipline as lab_summary: deterministic for a fixed store state and
``now``, values exactly as recorded, no interpretive vocabulary (enforced
by test), written only into the protected data home. Event ``notes`` are
deliberately NOT rendered — free text is never promoted into a factual
artifact (ACCEPTANCE H2 last item).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .. import analytics, config, store

TEMPLATE_VERSION = "1"
GENERATED_BY = f"cairn/health.event_response/t{TEMPLATE_VERSION}"


def build(conn, event_id: str, *, window_days: int = 90,
          now: datetime | None = None) -> tuple[str, str]:
    now = now or datetime.now(timezone.utc)
    data = analytics.event_response(conn, event_id, window_days=window_days)
    event = data["event"]
    result_hash = hashlib.sha256(json.dumps(
        data, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")).hexdigest()

    lines: list[str] = []
    lines.append("# Event window summary (factual)")
    lines.append("")
    lines.append(f"- generated_by: {GENERATED_BY}")
    lines.append(f"- generated_at: {now.isoformat()}")
    lines.append(f"- result_hash: {result_hash}")
    lines.append("")
    lines.append("## Event")
    lines.append("")
    lines.append(f"- id: {event['id']}")
    lines.append(f"- kind: {event['kind']}")
    if event["label"]:
        lines.append(f"- label: {event['label']}")
    lines.append(f"- start: {event['start_raw'] or 'unknown'}"
                 f" (precision: {event['time_precision']})")
    if event["end_raw"]:
        lines.append(f"- end: {event['end_raw']}")
    if event["dose_value"] is not None:
        lines.append(f"- dose: {event['dose_value']} {event['dose_unit']}")
    lines.append(f"- confidence: {event['confidence']}")
    lines.append(f"- window: ±{data['window_days']} days around the start")
    lines.append("")

    if "note" in data:
        lines.append(f"> {data['note']}")
        lines.append("")
        return "\n".join(lines), result_hash

    lines.append("## Before / after (numeric observations, as recorded)")
    lines.append("")
    lines.append("| metric | unit | before n | before min–max | before mean |"
                 " after n | after min–max | after mean |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for metric_id in sorted(data["metrics"]):
        m = data["metrics"][metric_id]
        b, a = m["before_summary"], m["after_summary"]

        def span(s):
            return (f"{s['min']}–{s['max']}" if s["n"] else "—")

        lines.append(
            f"| {metric_id} | {m['unit'] or ''} | {b['n']} | {span(b)} |"
            f" {b['mean'] if b['n'] else '—'} | {a['n']} | {span(a)} |"
            f" {a['mean'] if a['n'] else '—'} |"
        )
    lines.append("")

    uncertain = {k: m["in_start_window"] for k, m in data["metrics"].items()
                 if m["in_start_window"]}
    lines.append("## Observations inside the start-uncertainty window")
    lines.append("")
    if uncertain:
        for metric_id, points in sorted(uncertain.items()):
            for p in points:
                lines.append(f"- {metric_id} {p['date']}: {p['original']}"
                             " (not assigned to either side)")
    else:
        lines.append("- none")
    lines.append("")
    excluded = sum(m["non_numeric"] for m in data["metrics"].values())
    lines.append("## Limitations")
    lines.append("")
    lines.append("- bounded factual comparison of one event window; overlapping"
                 " events and measurement conditions are not accounted for")
    lines.append(f"- non-numeric/provisional observations excluded from"
                 f" summaries: {excluded}")
    lines.append("- this artifact records measurements as-is and draws no"
                 " conclusion from them")
    lines.append("")
    return "\n".join(lines), result_hash


def write(event_id: str, *, window_days: int = 90,
          home: Path | None = None, now: datetime | None = None) -> dict:
    home = home or config.resolve_home()
    conn = store.connect(home)
    try:
        markdown, result_hash = build(conn, event_id,
                                      window_days=window_days, now=now)
    finally:
        conn.close()
    out = home / "reports" / f"event-response-{event_id}.md"
    out.write_text(markdown, encoding="utf-8")
    config.protect_file(out)
    return {"path": str(out.relative_to(home)), "result_hash": result_hash}
