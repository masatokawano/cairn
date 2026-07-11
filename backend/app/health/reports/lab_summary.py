"""Deterministic factual laboratory report (H1, ACCEPTANCE.md H1 last item).

A data-verification artifact, NOT a clinical report: it lists what was
imported and from where, so representative records can be checked against
the source sheet. It contains no interpretation — no diagnosis, no trend
causality, no safe/dangerous statements; a template test enforces the
absence of that vocabulary.

Determinism: with a fixed store state and a fixed ``now``, the output bytes
are identical (rows fully ordered, hash over the factual rows included).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .. import config, store

TEMPLATE_VERSION = "1"
GENERATED_BY = f"cairn/health.lab_summary/t{TEMPLATE_VERSION}"


def build(conn, *, now: datetime | None = None) -> tuple[str, str]:
    """Return (markdown, result_hash) for the current store contents."""
    now = now or datetime.now(timezone.utc)

    sources = conn.execute(
        "SELECT original_name, sha256, size_bytes, status FROM source_files"
        " WHERE source_kind='labs_csv' ORDER BY original_name, sha256"
    ).fetchall()
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT observed_date FROM observations ORDER BY observed_date"
    ).fetchall()]
    metrics = conn.execute(
        "SELECT metric_id, count(*) FROM observations"
        " GROUP BY metric_id ORDER BY metric_id"
    ).fetchall()
    latest = conn.execute(
        "SELECT o.metric_id, o.observed_date, o.original_value,"
        "       o.original_unit, o.reference_text, o.quality_status,"
        "       o.source_name, o.id"
        " FROM observations o"
        " JOIN (SELECT metric_id, max(observed_date) AS d FROM observations"
        "       GROUP BY metric_id) m"
        "   ON o.metric_id = m.metric_id AND o.observed_date = m.d"
        " ORDER BY o.metric_id, o.fingerprint"
    ).fetchall()
    quarantine = conn.execute(
        "SELECT reason_code, count(*) FROM quarantine_records"
        " GROUP BY reason_code ORDER BY reason_code"
    ).fetchall()
    total_obs = conn.execute("SELECT count(*) FROM observations").fetchone()[0]
    provisional = conn.execute(
        "SELECT count(*) FROM observations WHERE quality_status='provisional'"
    ).fetchone()[0]

    # Missingness: metric × observed dates without a value.
    missing_lines = []
    for metric_id, _count in metrics:
        have = {r[0] for r in conn.execute(
            "SELECT DISTINCT observed_date FROM observations WHERE metric_id=?",
            [metric_id],
        ).fetchall()}
        gaps = [d.isoformat() for d in dates if d not in have]
        if gaps:
            missing_lines.append(f"- {metric_id}: {', '.join(gaps)}")

    factual_rows = "\n".join(
        f"{r[0]}|{r[1]}|{r[2]}|{r[3]}|{r[4]}|{r[5]}|{r[6]}" for r in latest
    )
    result_hash = hashlib.sha256(factual_rows.encode("utf-8")).hexdigest()

    lines: list[str] = []
    lines.append("# Laboratory data summary (factual)")
    lines.append("")
    lines.append(f"- generated_by: {GENERATED_BY}")
    lines.append(f"- generated_at: {now.isoformat()}")
    lines.append(f"- result_hash: {result_hash}")
    lines.append("")
    lines.append("## Source snapshots")
    lines.append("")
    for name, sha, size, status in sources:
        lines.append(f"- `{name}` sha256={sha[:16]} size={size} status={status}")
    lines.append("")
    lines.append("## Coverage")
    lines.append("")
    lines.append(f"- observations: {total_obs} (provisional: {provisional})")
    lines.append(f"- metrics: {len(metrics)}")
    lines.append(
        "- measurement dates: "
        + (", ".join(d.isoformat() for d in dates) if dates else "none")
    )
    lines.append("")
    lines.append("## Latest values per metric")
    lines.append("")
    lines.append("| metric | date | value (as recorded) | unit (as recorded) | reference | quality | source |")
    lines.append("|---|---|---|---|---|---|---|")
    for metric_id, d, value, unit, ref, quality, source_name, _oid in latest:
        lines.append(
            f"| {metric_id} | {d.isoformat()} | {value} | {unit or ''} |"
            f" {ref or ''} | {quality} | {source_name} |"
        )
    lines.append("")
    lines.append("## Missingness")
    lines.append("")
    lines.extend(missing_lines or ["- none: every metric has a value on every measurement date"])
    lines.append("")
    lines.append("## Quarantine")
    lines.append("")
    if quarantine:
        for reason, count in quarantine:
            lines.append(f"- {reason}: {count} record(s) — resolve by extending the alias catalog")
    else:
        lines.append("- empty")
    lines.append("")
    lines.append("## Provenance")
    lines.append("")
    lines.append(
        "- every observation row carries source_file_id, source_row_ref and a"
        " deterministic fingerprint; verify any value above against the"
        " snapshot in raw/ via its source_row_ref"
    )
    lines.append("")
    lines.append("## Limitations")
    lines.append("")
    lines.append(
        "- this is a data-verification artifact: values are listed exactly as"
        " recorded, without interpretation of any kind"
    )
    lines.append("- provisional rows have units this catalog version cannot normalize")
    lines.append("")
    return "\n".join(lines), result_hash


def write(home: Path | None = None, *, now: datetime | None = None) -> dict:
    """Generate and store the report inside the protected data home."""
    home = home or config.resolve_home()
    conn = store.connect(home)
    try:
        markdown, result_hash = build(conn, now=now)
    finally:
        conn.close()
    out = home / "reports" / "lab-summary.md"
    out.write_text(markdown, encoding="utf-8")
    config.protect_file(out)
    return {"path": str(out.relative_to(home)), "result_hash": result_hash}
