"""Backup / restore / integrity / retention / deletion (H8, ADR-0005).

Operational trust for a store that must survive years (H-S6). Everything here
treats the live store as read-only except the two explicitly destructive
functions (``delete_derived``, ``purge``), which enumerate before they act and
require an explicit confirmation token — no silent data loss (ACCEPTANCE H8,
AGENTS.md invariant 8).

Backup format: a single ``.tar.gz`` containing the store DB, the immutable
``raw/`` snapshots, ``reports/``, ``derived/``, ``quarantine/`` and a
``MANIFEST.json``. The manifest records schema/catalog/mapping versions, table
row counts, and a sha256 for the store file and every raw file, so a restore
into an empty environment can prove it reproduced counts and hashes. The
``backups/`` subdir (old premigrate DB copies) is excluded so backups don't
nest.

Encryption: the archive is written plaintext. The DEFAULT data home sits on a
FileVault-protected disk; any backup copied OFF this machine must go to an
encrypted destination (PRIVACY.md §10). ``backup`` refuses a destination
inside the git worktree.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import config, schema, store

logger = logging.getLogger("cairn.health")

MANIFEST_NAME = "MANIFEST.json"
BACKUP_PREFIX = "health-backup-"
# Enumerated by purge so the user sees exactly what a destructive delete hits.
PURGE_DIRS = ("raw", "store", "derived", "reports", "quarantine", "backups")
_COUNTED_TABLES = (
    "source_files", "import_runs", "observations", "events", "documents",
    "interpretations", "interpretation_evidence", "data_snapshots",
    "quarantine_records", "metric_catalog", "metric_aliases",
)
# Archived content (backups/ excluded to avoid nesting old DB copies).
_ARCHIVED_SUBDIRS = ("store", "raw", "reports", "derived", "quarantine")


class OpsError(Exception):
    pass


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _table_counts(home: Path) -> dict:
    conn = store.connect_readonly(home)
    try:
        return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in _COUNTED_TABLES}
    finally:
        conn.close()


def _versions(home: Path) -> dict:
    conn = store.connect_readonly(home)
    try:
        cat = conn.execute(
            "SELECT catalog_version FROM metric_catalog LIMIT 1").fetchone()
        mapping = conn.execute(
            "SELECT mapping_version FROM metric_aliases LIMIT 1").fetchone()
        return {"schema_version": schema.SCHEMA_VERSION,
                "catalog_version": cat[0] if cat else None,
                "mapping_version": mapping[0] if mapping else None}
    finally:
        conn.close()


def _build_manifest(home: Path) -> dict:
    store_file = config.store_path(home)
    raw_hashes = {}
    raw_root = home / "raw"
    if raw_root.exists():
        for p in sorted(raw_root.rglob("*")):
            if p.is_file():
                raw_hashes[str(p.relative_to(home))] = _sha256_file(p)
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "versions": _versions(home),
        "table_counts": _table_counts(home),
        "store_sha256": _sha256_file(store_file),
        "raw_sha256": raw_hashes,
    }


def backup(home: Path | None = None, dest_dir: Path | None = None) -> dict:
    """Write a consistent .tar.gz snapshot. Read-only w.r.t. the live store —
    a failure here never mutates the store or a prior good import."""
    home = home or config.resolve_home()
    if not config.store_path(home).exists():
        raise OpsError("store not initialized; nothing to back up")
    dest_dir = Path(dest_dir) if dest_dir else home / "backups"
    if _inside_worktree(dest_dir):
        raise OpsError(f"refusing to write a health backup inside the git "
                       f"worktree: {dest_dir}")
    dest_dir.mkdir(parents=True, exist_ok=True)

    manifest = _build_manifest(home)
    # Microsecond precision so two backups in the same second don't collide
    # (and silently overwrite each other).
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    final = dest_dir / f"{BACKUP_PREFIX}{stamp}.tar.gz"
    # Write to a temp file first, fsync, then atomically rename — a crash
    # mid-write leaves no half archive masquerading as good.
    tmp_fd, tmp_name = tempfile.mkstemp(suffix=".tar.gz.part", dir=str(dest_dir))
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        with tarfile.open(tmp_path, "w:gz") as tar:
            for name in _ARCHIVED_SUBDIRS:
                sub = home / name
                if sub.exists():
                    tar.add(sub, arcname=name)
            man_bytes = json.dumps(manifest, ensure_ascii=False,
                                   indent=2).encode("utf-8")
            info = tarfile.TarInfo(MANIFEST_NAME)
            info.size = len(man_bytes)
            import io
            tar.addfile(info, io.BytesIO(man_bytes))
        os.chmod(tmp_path, config.FILE_MODE)
        # Durable rename: flush the archive to disk, then rename, then flush
        # the directory entry — so a crash cannot leave a good-looking but
        # partial archive.
        with open(tmp_path, "rb") as fh:
            os.fsync(fh.fileno())
        tmp_path.replace(final)
        dir_fd = os.open(str(dest_dir), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    logger.info("health backup written stamp=%s obs=%d", stamp,
                manifest["table_counts"]["observations"])
    return {"archive": str(final),
            "created_at": manifest["created_at"],
            "table_counts": manifest["table_counts"],
            "versions": manifest["versions"]}


def read_manifest(archive: Path) -> dict:
    with tarfile.open(archive, "r:gz") as tar:
        member = tar.extractfile(MANIFEST_NAME)
        if member is None:
            raise OpsError("archive has no MANIFEST.json")
        return json.loads(member.read().decode("utf-8"))


def restore(archive: Path | None, into: Path, *, verify: bool = True) -> dict:
    """Extract an archive into an EMPTY home and (default) verify EVERY
    manifest hash (store + each raw file), the versions, and table counts.

    The manifest lives inside the archive, so this proves the restore is
    complete and uncorrupted — it is an integrity checksum, not a signature,
    and does not defend against a maliciously re-packed archive (make backups
    yourself and store them on trusted media)."""
    archive = Path(archive)
    into = Path(into)
    if _inside_worktree(into):
        raise OpsError(f"refusing to restore health data inside the git "
                       f"worktree: {into}")
    if into.exists() and any(into.iterdir()):
        raise OpsError(f"restore target must be empty: {into}")
    into.mkdir(parents=True, exist_ok=True)
    os.chmod(into, config.DIR_MODE)

    manifest = read_manifest(archive)
    with tarfile.open(archive, "r:gz") as tar:
        members = [m for m in tar.getmembers() if _safe_member(m, into)]
        tar.extractall(into, members=members, filter="data")
    for name in config.SUBDIRS:
        (into / name).mkdir(exist_ok=True)
        os.chmod(into / name, config.DIR_MODE)
    sf = config.store_path(into)
    if sf.exists():
        os.chmod(sf, config.FILE_MODE)

    result = {"restored_to": str(into), "manifest_created_at":
              manifest["created_at"], "ok": True, "mismatches": []}
    if verify:
        result["mismatches"] = _verify_against_manifest(into, manifest)
        result["ok"] = not result["mismatches"]
    logger.info("health restore ok=%s into=%s", result["ok"], into.name)
    return result


def _verify_against_manifest(home: Path, manifest: dict) -> list[str]:
    """Every store/raw hash, version, and table count must match. Returns the
    list of mismatches (empty = fully verified)."""
    mism: list[str] = []
    sf = config.store_path(home)
    if not sf.exists():
        return ["store_missing"]
    if _sha256_file(sf) != manifest["store_sha256"]:
        mism.append("store_sha256")
    for rel, want in manifest.get("raw_sha256", {}).items():
        p = home / rel
        if not p.exists():
            mism.append(f"raw_missing:{rel}")
        elif _sha256_file(p) != want:
            mism.append(f"raw_sha256:{rel}")
    if _versions(home) != manifest["versions"]:
        mism.append("versions")
    counts = _table_counts(home)
    for t, n in manifest["table_counts"].items():
        if counts.get(t) != n:
            mism.append(f"count:{t}")
    return mism


def verify_backup(archive: Path) -> dict:
    """Extract to a throwaway dir and check the store hash AND every raw-file
    hash against the manifest. Full integrity probe without touching a home."""
    archive = Path(archive)
    manifest = read_manifest(archive)
    with tempfile.TemporaryDirectory() as td:
        target = Path(td)
        with tarfile.open(archive, "r:gz") as tar:
            members = [m for m in tar.getmembers() if _safe_member(m, target)]
            tar.extractall(target, members=members, filter="data")
        mism = []
        sf = target / "store" / "health.duckdb"
        if not sf.exists() or _sha256_file(sf) != manifest["store_sha256"]:
            mism.append("store_sha256")
        for rel, want in manifest.get("raw_sha256", {}).items():
            p = target / rel
            if not p.exists() or _sha256_file(p) != want:
                mism.append(f"raw:{rel}")
    return {"ok": not mism, "mismatches": mism,
            "created_at": manifest["created_at"],
            "table_counts": manifest["table_counts"]}


def list_backups(dest_dir: Path | None = None) -> list[dict]:
    home = config.resolve_home()
    dest_dir = Path(dest_dir) if dest_dir else home / "backups"
    out = []
    if dest_dir.exists():
        for p in sorted(dest_dir.glob(f"{BACKUP_PREFIX}*.tar.gz")):
            out.append({"archive": str(p), "size_bytes": p.stat().st_size,
                        "modified": datetime.fromtimestamp(
                            p.stat().st_mtime, timezone.utc).isoformat()})
    return out


def rotate_backups(dest_dir: Path | None = None, *, keep: int = 7) -> dict:
    """Keep the newest ``keep`` backup archives; remove older ones. Only ever
    deletes files matching the backup naming pattern."""
    if keep < 1:
        raise OpsError("keep must be >= 1")
    home = config.resolve_home()
    dest_dir = Path(dest_dir) if dest_dir else home / "backups"
    if dest_dir.is_symlink():
        raise OpsError(f"refusing to rotate a symlinked backup dir: {dest_dir}")
    # Only ever touch real files matching the backup name pattern.
    archives = sorted(
        (p for p in dest_dir.glob(f"{BACKUP_PREFIX}*.tar.gz")
         if p.is_file() and not p.is_symlink()),
        key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for p in archives[keep:]:
        p.unlink()
        removed.append(str(p))
    logger.info("health backup rotation kept=%d removed=%d", min(keep,
                len(archives)), len(removed))
    return {"kept": archives[:keep] and [str(p) for p in archives[:keep]],
            "removed": removed}


# --- destructive (guarded) ----------------------------------------------------

def delete_derived(home: Path | None = None) -> dict:
    """Remove regenerable derived data (derived/ + reports/). Sources, store
    and events are untouched — this is the safe, reversible-by-rebuild delete."""
    home = home or config.resolve_home()
    removed = []
    for name in ("derived", "reports"):
        d = home / name
        # A symlinked derived/ or reports/ could point at raw/ or outside the
        # home; refuse to follow it rather than deleting the target's contents.
        if d.is_symlink():
            raise OpsError(f"refusing to delete through a symlinked {name}/")
        if d.exists():
            for p in d.iterdir():
                if p.is_symlink():
                    p.unlink()                       # remove the link, not target
                    removed.append(str(p.relative_to(home)) + "@")
                elif p.is_file():
                    p.unlink()
                    removed.append(str(p.relative_to(home)))
                elif p.is_dir():
                    shutil.rmtree(p)
                    removed.append(str(p.relative_to(home)) + "/")
    logger.info("health delete_derived removed=%d", len(removed))
    return {"removed": removed,
            "note": "reports regenerate via `cairn health report`/`deliver`; "
                    "derived aggregates via re-query"}


def purge_plan(home: Path | None = None) -> dict:
    """Enumerate everything a destructive purge would delete — WITHOUT
    deleting. What the user is shown before confirming (ACCEPTANCE H8)."""
    home = home or config.resolve_home()

    def _dir_stats(d: Path) -> dict:
        files = [p for p in d.rglob("*") if p.is_file()] if d.exists() else []
        return {"exists": d.exists(), "files": len(files),
                "bytes": sum(p.stat().st_size for p in files)}

    return {"home": str(home),
            "directories": {name: _dir_stats(home / name)
                            for name in PURGE_DIRS},
            "confirmation_required": "pass confirm=<the home path> to purge"}


def purge(home: Path | None = None, *, confirm: str | None = None) -> dict:
    """Irreversibly delete the ENTIRE health data home. Requires ``confirm``
    to equal the resolved home path (belt-and-suspenders; the CLI adds its own
    prompt). AGENTS.md invariant 8: never call this on real data without an
    explicit per-execution human approval."""
    home = home or config.resolve_home()
    if confirm != str(home):
        raise OpsError(
            "purge refused: pass confirm equal to the exact home path; "
            f"expected {str(home)!r}")
    plan = purge_plan(home)
    shutil.rmtree(home)
    logger.warning("health data home PURGED path=%s", home.name)
    return {"purged": str(home), "was": plan["directories"]}


# --- helpers ------------------------------------------------------------------

def _inside_worktree(path: Path) -> bool:
    from .config import _inside_git_worktree

    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    return _inside_git_worktree(resolved) is not None


def _safe_member(member: tarfile.TarInfo, dest: Path) -> bool:
    # Reject absolute paths, traversal, and any link members (sym or hard).
    # Proper path containment — not a string prefix (which would let
    # /tmp/restore-evil pass for dest /tmp/restore).
    if member.issym() or member.islnk():
        return False
    name = member.name
    if name.startswith("/") or ".." in Path(name).parts:
        return False
    target = (dest / name).resolve()
    try:
        return target.is_relative_to(dest.resolve())
    except AttributeError:  # py<3.9 (not expected here)
        return str(target) == str(dest.resolve()) or \
            str(target).startswith(str(dest.resolve()) + os.sep)
