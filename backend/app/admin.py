"""Management CLI for Cairn (separate from the MCP server by design).

Commands:
  redact-scan    Dry-run: report per-provider secret counts in the DB.
  redact-apply   Destructive: redact all stored messages/titles in place.
                 Makes a timestamped backup first. Rebuilds FTS, recomputes
                 content hashes, truncates the WAL, VACUUMs, then verifies
                 no pattern remains in rows or raw DB file bytes.
  force-resync   Clear ingest_files state so the next CLI sync re-parses
                 every log file (re-ingest goes through redaction).
  import-runs    Print recent import history (counts, warnings, status).
  integrity-check  Read-only DB consistency audit (exit 2 if problems found).
  backup         Write a consistent single-file copy of the DB (contains
                 plaintext; locked to 0600). Restore by copying it back or
                 pointing CAIRN_DB at it.
  export-jsonl   Stream conversations as JSONL (machine-readable) with
                 source / date-range / conversation_id filters. Writes to
                 --out (0600) or stdout (status line on stderr so the
                 output stays pipe-clean).
  export-markdown  Same filters as export-jsonl but renders human-readable
                 Markdown (one `# title` section per conversation, role
                 headings, `---` between conversations).
  rechunk        (Re)generate semantic-search chunks from messages (P2-1a).
                 Default: only messages missing chunks at the current version.
                 --all forces regeneration (use after a chunking/redaction
                 change). Chunks are derived data, regenerable from messages.
  reindex        (Re)generate embeddings for chunks (P2-1b). Default is
                 --missing (skip chunks already embedded by this provider+
                 model). --all overwrites. --provider/--model select the
                 EmbeddingProvider (default: local-sbert with
                 intfloat/multilingual-e5-small). Embeddings are derived data.

Usage:
  .venv/bin/python -m app.admin redact-scan
  .venv/bin/python -m app.admin redact-apply [--yes]
  .venv/bin/python -m app.admin force-resync
  .venv/bin/python -m app.admin import-runs [--limit N] [--source S]
  .venv/bin/python -m app.admin integrity-check
  .venv/bin/python -m app.admin backup [--out PATH]
  .venv/bin/python -m app.admin export-jsonl [--out PATH] [--source S]
                                             [--after ISO] [--before ISO]
                                             [--conversation-id N]
  .venv/bin/python -m app.admin export-markdown [--out PATH] [--source S]
                                                 [--after ISO] [--before ISO]
                                                 [--conversation-id N]
  .venv/bin/python -m app.admin rechunk [--all | --version-mismatched]
  .venv/bin/python -m app.admin reindex [--provider P] [--model M] [--all | --missing]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import db, redact
from .parsers.base import ParsedConversation, ParsedMessage


def _scan_db() -> dict:
    """Count secrets per provider across messages and titles (no changes)."""
    conn = db.connect()
    provider_counts: dict[str, int] = {}
    affected_conversations: set[int] = set()
    affected_messages = 0
    for row in conn.execute("SELECT id, conversation_id, text FROM messages"):
        counts = redact.scan(row["text"])
        if counts:
            affected_messages += 1
            affected_conversations.add(row["conversation_id"])
            for k, v in counts.items():
                provider_counts[k] = provider_counts.get(k, 0) + v
    for row in conn.execute("SELECT id, title FROM conversations"):
        if redact.redact_title(row["title"]) != row["title"]:
            affected_conversations.add(row["id"])
            provider_counts["title"] = provider_counts.get("title", 0) + 1
    return {
        "providers": provider_counts,
        "affected_messages": affected_messages,
        "affected_conversations": len(affected_conversations),
        "conversation_ids": sorted(affected_conversations),
    }


def _recompute_hash(conn, conv_id: int) -> None:
    rows = conn.execute(
        "SELECT role, text, created_at FROM messages WHERE conversation_id=? ORDER BY idx",
        (conv_id,),
    ).fetchall()
    pc = ParsedConversation(
        source="", source_id="", title="",
        messages=[ParsedMessage(role=r["role"], text=r["text"], created_at=r["created_at"]) for r in rows],
    )
    conn.execute("UPDATE conversations SET content_hash=? WHERE id=?", (pc.content_hash(), conv_id))


def _verify_clean(db_path: str) -> list[str]:
    """Return problems found; empty list means clean."""
    problems = []
    conn = db.connect()
    for row in conn.execute("SELECT id, text FROM messages"):
        if redact.scan(row["text"]):
            problems.append(f"message {row['id']} still contains a secret pattern")
    for row in conn.execute("SELECT id, title FROM conversations"):
        if redact.redact_title(row["title"]) != row["title"]:
            problems.append(f"conversation {row['id']} title still contains a secret pattern")
    # Raw file bytes: after wal_checkpoint(TRUNCATE) + VACUUM nothing should
    # linger in free pages or sidecar files.
    for suffix in ("", "-wal", "-shm"):
        path = db_path + suffix
        if not os.path.exists(path):
            continue
        with open(path, "rb") as f:
            blob = f.read().decode("utf-8", errors="replace")
        counts = redact.scan(blob)
        if counts:
            problems.append(f"{os.path.basename(path)} raw bytes match patterns: {counts}")
    return problems


def cmd_scan(_args) -> int:
    report = _scan_db()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_apply(args) -> int:
    db_path = os.path.abspath(db.DB_PATH)
    if not os.path.exists(db_path):
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 1

    report = _scan_db()
    print("dry-run:", json.dumps(report, ensure_ascii=False))
    if not report["affected_conversations"]:
        print("nothing to redact")
        return 0
    if not args.yes:
        answer = input(
            f"{report['affected_conversations']} conversations / "
            f"{report['affected_messages']} messages will be rewritten. Proceed? [y/N] "
        )
        if answer.strip().lower() != "y":
            print("aborted")
            return 1

    conn = db.connect()

    # 1. Backup before the destructive rewrite (shared with the `backup` cmd).
    backup_path = db.backup()
    print(f"backup: {backup_path}")

    # 2. Rewrite messages and titles (messages_au trigger keeps FTS in sync,
    #    but we rebuild afterwards anyway for belt-and-braces consistency).
    with conn:
        for row in conn.execute("SELECT id, text FROM messages").fetchall():
            new_text = redact.redact(row["text"])
            if new_text != row["text"]:
                conn.execute("UPDATE messages SET text=? WHERE id=?", (new_text, row["id"]))
        for row in conn.execute("SELECT id, title FROM conversations").fetchall():
            new_title = redact.redact_title(row["title"])
            if new_title != row["title"]:
                conn.execute("UPDATE conversations SET title=? WHERE id=?", (new_title, row["id"]))
        # 3. Recompute content_hash from redacted text so future re-syncs
        #    (which redact at ingest) compare equal and skip.
        for conv_id in report["conversation_ids"]:
            _recompute_hash(conn, conv_id)
        # 4. Rebuild the FTS index from the messages table.
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")

    # 5. Flush WAL and compact so old plaintext pages don't survive on disk.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # 6. Verify.
    problems = _verify_clean(db_path)
    if problems:
        print("VERIFICATION FAILED:", file=sys.stderr)
        for p in problems:
            print(" -", p, file=sys.stderr)
        return 2
    print("verification OK: no secret patterns in rows or raw DB/WAL/SHM bytes")
    print("note: the backup file retains the original plaintext — "
          "delete it once you have confirmed the migration:", backup_path)
    return 0


def cmd_force_resync(_args) -> int:
    conn = db.connect()
    with conn:
        n = conn.execute("DELETE FROM ingest_files").rowcount
    print(f"cleared {n} ingest_files entries; next sync will re-parse all CLI logs")
    from . import cli_sync
    stats = cli_sync.scan_once()
    print(json.dumps({k: v for k, v in stats.items() if k != "warnings"}, ensure_ascii=False))
    if stats["warnings"]:
        print(f"warnings: {len(stats['warnings'])} (first: {stats['warnings'][0]})")
    return 0


def cmd_import_runs(args) -> int:
    runs = db.list_import_runs(limit=args.limit, source=args.source)
    print(json.dumps(runs, ensure_ascii=False, indent=2))
    return 0


def cmd_integrity_check(_args) -> int:
    report = db.integrity_check()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


def cmd_backup(args) -> int:
    db_path = os.path.abspath(db.DB_PATH)
    if not os.path.exists(db_path):
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 1
    path = db.backup(args.out)
    print(f"backup: {path}")
    print("note: the backup contains plaintext conversation data (0600). "
          "Restore by copying it back or setting CAIRN_DB to it.")
    return 0


def _run_export(writer, args) -> int:
    """Drive the shared export path: --out file (0600) or stdout, with the
    status line always on stderr so the artifact stays pipe-clean."""
    filters = dict(
        source=args.source, after=args.after, before=args.before,
        conversation_id=args.conversation_id,
    )
    if args.out:
        out_path = os.path.abspath(args.out)
        with open(out_path, "w", encoding="utf-8") as f:
            n = writer(f, **filters)
        try:
            os.chmod(out_path, 0o600)  # contains plaintext conversation data
        except OSError:
            pass
        print(f"exported {n} conversations → {out_path}", file=sys.stderr)
    else:
        n = writer(sys.stdout, **filters)
        print(f"exported {n} conversations", file=sys.stderr)
    return 0


def cmd_export_jsonl(args) -> int:
    return _run_export(db.export_jsonl, args)


def cmd_export_markdown(args) -> int:
    return _run_export(db.export_markdown, args)


def cmd_rechunk(args) -> int:
    stats = db.rechunk_messages(force=args.all)
    print(json.dumps(
        {"chunking_version": db.CURRENT_CHUNKING_VERSION, **stats},
        ensure_ascii=False, indent=2,
    ))
    return 0


def _make_provider(name: str, model: str | None):
    """Resolve a provider name (CLI string) to a concrete EmbeddingProvider.
    Imports are lazy so we don't pull sentence-transformers when a different
    provider is chosen (or when this function is only checking validity)."""
    if name == "local-sbert":
        from .embedding.local_sbert import DEFAULT_MODEL, LocalSbertProvider
        return LocalSbertProvider(model=model or DEFAULT_MODEL)
    raise SystemExit(f"unknown provider: {name!r} (known: local-sbert)")


def cmd_reindex(args) -> int:
    provider = _make_provider(args.provider, args.model)
    stats = db.embed_chunks(provider, only_missing=not args.all)
    # provider.dimension may load the model when it isn't in the static table;
    # for the default e5-small it is, so this stays a metadata-only call.
    print(json.dumps({
        "provider": provider.name,
        "model": provider.model,
        "dimension": provider.dimension,
        **stats,
    }, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cairn-admin", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("redact-scan").set_defaults(func=cmd_scan)
    p_apply = sub.add_parser("redact-apply")
    p_apply.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    p_apply.set_defaults(func=cmd_apply)
    sub.add_parser("force-resync").set_defaults(func=cmd_force_resync)
    p_runs = sub.add_parser("import-runs")
    p_runs.add_argument("--limit", type=int, default=20)
    p_runs.add_argument("--source", default=None, help="filter: upload | claude_cli | codex_cli")
    p_runs.set_defaults(func=cmd_import_runs)
    sub.add_parser("integrity-check").set_defaults(func=cmd_integrity_check)
    p_backup = sub.add_parser("backup")
    p_backup.add_argument("--out", default=None, help="destination path (default: <db>.backup-<timestamp>)")
    p_backup.set_defaults(func=cmd_backup)
    def _add_export_filters(sp):
        sp.add_argument("--out", default=None, help="destination path (default: stdout)")
        sp.add_argument("--source", default=None, help="filter: chatgpt | claude | claude_cli | codex_cli | gemini")
        sp.add_argument("--after", default=None, help="updated_at >= ISO8601 (inclusive)")
        sp.add_argument("--before", default=None, help="updated_at <= ISO8601 (inclusive)")
        sp.add_argument("--conversation-id", type=int, default=None, dest="conversation_id",
                        help="export only this conversation (DB rowid)")
    p_export = sub.add_parser("export-jsonl")
    _add_export_filters(p_export)
    p_export.set_defaults(func=cmd_export_jsonl)
    p_md = sub.add_parser("export-markdown")
    _add_export_filters(p_md)
    p_md.set_defaults(func=cmd_export_markdown)
    p_rechunk = sub.add_parser("rechunk")
    g_rechunk = p_rechunk.add_mutually_exclusive_group()
    g_rechunk.add_argument("--all", action="store_true",
                           help="regenerate chunks for every message at the current version")
    g_rechunk.add_argument("--version-mismatched", action="store_true",
                           help="(default) chunk only messages missing the current version")
    p_rechunk.set_defaults(func=cmd_rechunk)
    p_reindex = sub.add_parser("reindex")
    p_reindex.add_argument("--provider", default="local-sbert",
                           help="EmbeddingProvider name (default: local-sbert)")
    p_reindex.add_argument("--model", default=None,
                           help="provider-specific model id (default: provider's default)")
    g_reindex = p_reindex.add_mutually_exclusive_group()
    g_reindex.add_argument("--all", action="store_true",
                           help="re-embed every chunk (overwrite existing rows for this provider+model)")
    g_reindex.add_argument("--missing", action="store_true",
                           help="(default) embed only chunks without a row for this provider+model")
    p_reindex.set_defaults(func=cmd_reindex)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
