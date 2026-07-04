"""Read-only Obsidian vault connector (M3, DESIGN.md §5.1).

Notes → items(kind='note', source='obsidian', external_id=vault-relative
path). Indexed directories (§4): ``10 Themes``, ``20 Projects``,
``00 Inbox/Ideas``, ``50 Decisions`` under the External Brain folder.
``90 Auto`` and ``40 Reviews`` are NEVER indexed — they contain Cairn's own
output, and indexing them would feed generated text back into retrieval
(§4: 自己生成物の還流ループを防ぐ).

This module never writes to the vault (invariant 1). All vault writes in the
codebase go through deliver/obsidian_writer.py exclusively (invariant 2).

Config: ``CAIRN_OBSIDIAN_VAULT`` (vault root, e.g. ~/Obsidian; required),
``CAIRN_EXTERNAL_BRAIN_DIR`` (default "External Brain").

Diff model: a full directory listing every run (personal-vault scale), with
an mtime prefilter — files older than the stored cursor that are already
registered are skipped without reading; everything else is read and left to
upsert_items' content_hash to dedupe. Notes deleted or renamed in the vault
are pruned from the registry after a successful scan (renames would
otherwise leave ghost items forever, since external_id is the path).
"""
from __future__ import annotations

import os
from pathlib import Path

from .. import db
from . import ConnectorError, index_changed_items

SOURCE = "obsidian"
INDEX_DIRS = ("10 Themes", "20 Projects", "00 Inbox/Ideas", "50 Decisions")
# Safety margin subtracted from the cursor for the mtime prefilter: iCloud /
# sync tools sometimes materialise files with slightly stale mtimes.
MTIME_MARGIN_S = 3600.0
# Cap per note kept in meta.text (index excerpt, not an archive copy).
TEXT_CAP = 50_000


def _vault_root() -> Path:
    vault = os.environ.get("CAIRN_OBSIDIAN_VAULT")
    if not vault:
        raise ConnectorError("CAIRN_OBSIDIAN_VAULT is not set")
    root = Path(vault).expanduser()
    if not root.is_dir():
        raise ConnectorError(f"vault not found: {root}")
    return root


def _iter_note_paths(root: Path, brain_dir: str):
    for sub in INDEX_DIRS:
        base = root / brain_dir / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            if any(part.startswith(".") for part in path.relative_to(root).parts):
                continue
            yield path


def _to_record(root: Path, path: Path, warnings: list[str]) -> dict | None:
    try:
        stat = path.stat()
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        warnings.append(f"{path.relative_to(root)}: {type(exc).__name__}")
        return None
    rel = path.relative_to(root).as_posix()
    from datetime import datetime, timezone

    def iso(ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

    created = getattr(stat, "st_birthtime", None) or stat.st_mtime
    parts = path.relative_to(root).parts
    return {
        "external_id": rel,
        "title": path.stem,
        "url": None,
        "created_at": iso(created),
        "updated_at": iso(stat.st_mtime),
        "meta": {
            # folder = the indexed subdir (e.g. "10 Themes"), for filtering
            # and for the 90 Auto context list
            "folder": "/".join(parts[1:-1]) if len(parts) > 2 else "",
            "text": text[:TEXT_CAP],
        },
    }


def sync() -> dict:
    """Scan the vault into the items registry. Returns stats.

    On failure the error is recorded in sync_state.last_error and re-raised;
    registry pruning only happens after a fully successful scan, so a partial
    listing can never mass-delete note items.
    """
    state = db.get_sync_state(SOURCE)
    last_scan = (state or {}).get("cursor", {}).get("last_scan_at")

    try:
        root = _vault_root()
        brain_dir = os.environ.get("CAIRN_EXTERNAL_BRAIN_DIR", "External Brain")
        existing = {
            r["external_id"]
            for r in db.connect().execute(
                "SELECT external_id FROM items WHERE source = ?", (SOURCE,)
            )
        }
        from datetime import datetime, timezone
        cutoff = None
        if last_scan:
            cutoff = (
                datetime.fromisoformat(last_scan).timestamp() - MTIME_MARGIN_S
            )
        scan_started = datetime.now(timezone.utc).isoformat()

        warnings: list[str] = []
        seen: list[str] = []
        records: list[dict] = []
        prefiltered = 0
        for path in _iter_note_paths(root, brain_dir):
            rel = path.relative_to(root).as_posix()
            seen.append(rel)
            if cutoff is not None and rel in existing:
                try:
                    if path.stat().st_mtime < cutoff:
                        prefiltered += 1
                        continue
                except OSError:
                    pass  # fall through to the full read (which records it)
            rec = _to_record(root, path, warnings)
            if rec is not None:
                records.append(rec)

        stats = db.upsert_items(SOURCE, "note", records)
        pruned = db.prune_items(SOURCE, keep_external_ids=seen)
    except Exception as exc:
        db.set_sync_state(SOURCE, error=f"{type(exc).__name__}: {exc}")
        raise

    index_stats = index_changed_items(stats["changed_ids"]) if stats["changed_ids"] else None
    links = db.rebuild_item_links() if stats["changed_ids"] or pruned else None
    db.set_sync_state(SOURCE, cursor={"last_scan_at": scan_started})
    return {
        "source": SOURCE,
        "scanned": len(seen),
        "prefiltered": prefiltered,
        "inserted": stats["inserted"],
        "updated": stats["updated"],
        "skipped": stats["skipped"] + prefiltered,
        "pruned": pruned,
        "warnings": warnings,
        "index": index_stats,
        "links": links,
    }
