"""`cairn` CLI (DESIGN.md §5.7).

Three top-level command groups, all wired: `sync` (M0 conversations, M1
karakeep/zotero, M3 obsidian + 90 Auto lists), `index rebuild` (M2), and
`review weekly` (M4).

Entry point: `backend/bin/cairn` (a shell wrapper that sets PYTHONPATH and
execs `python -m app.cli`). Editable install / pyproject.toml is deferred
to M6; DESIGN.md §5.7 and the M0 plan explicitly cover this trade-off.
"""
from __future__ import annotations

import json

import typer

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Cairn integration CLI (sync / review / index).",
)

sync_app = typer.Typer(
    no_args_is_help=True,
    help="Pull data into Cairn (conversations / karakeep / zotero; obsidian lands in M3).",
)
review_app = typer.Typer(no_args_is_help=True, help="Weekly review (M4).")
index_app = typer.Typer(no_args_is_help=True, help="Derived-data rebuilds (M2/M6).")

app.add_typer(sync_app, name="sync")
app.add_typer(review_app, name="review")
app.add_typer(index_app, name="index")


@sync_app.command("conversations")
def sync_conversations() -> None:
    """Sync claude / codex CLI session logs into cairn.db (Phase 1 behaviour)."""
    from . import cli_sync
    stats = cli_sync.scan_once()
    typer.echo(json.dumps(stats, ensure_ascii=False, indent=2))


def _run_connector(fn) -> None:
    """Run a connector sync; JSON stats on stdout, non-zero exit on failure.

    The connector has already recorded the failure in sync_state.last_error
    (DESIGN.md §5.1); here we only surface it and set the exit code."""
    try:
        stats = fn()
    except Exception as exc:
        typer.echo(f"sync failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(json.dumps(stats, ensure_ascii=False, indent=2))


@sync_app.command("karakeep")
def sync_karakeep(
    full: bool = typer.Option(
        False, "--full",
        help="Sweep every page (picks up edits to old bookmarks), not just new ones.",
    ),
) -> None:
    """Pull Karakeep bookmarks into the items registry (M1)."""
    from .connectors import karakeep
    _run_connector(lambda: karakeep.sync(full=full))


@sync_app.command("zotero")
def sync_zotero(
    full: bool = typer.Option(
        False, "--full", help="Ignore the stored library-version cursor and refetch everything.",
    ),
) -> None:
    """Pull Zotero bibliography into the items registry (M1)."""
    from .connectors import zotero
    _run_connector(lambda: zotero.sync(full=full))


@sync_app.command("obsidian")
def sync_obsidian() -> None:
    """Index the Obsidian vault into the items registry (M3, read-only)."""
    from .connectors import obsidian
    _run_connector(obsidian.sync)


@sync_app.command("all")
def sync_all() -> None:
    """Sync every source, then write the 90 Auto lists (M3, DESIGN.md §5.7).

    One failing source must not starve the others (S4: 放置しても壊れない) —
    each failure is recorded and the command exits non-zero at the end.
    Connector failures are also in sync_state.last_error already.
    """
    from . import cli_sync
    from .connectors import karakeep, obsidian, zotero

    steps = [
        ("conversations", cli_sync.scan_once),
        ("karakeep", karakeep.sync),
        ("zotero", zotero.sync),
        ("obsidian", obsidian.sync),
    ]
    out: dict = {"sources": {}, "failed": []}
    for name, fn in steps:
        try:
            out["sources"][name] = fn()
        except Exception as exc:
            out["sources"][name] = f"failed: {type(exc).__name__}: {exc}"
            out["failed"].append(name)

    # 90 Auto lists: regenerate whenever the vault is configured — cheap, and
    # the lists must reflect deletions/edits even on all-skip syncs.
    try:
        from .deliver import auto_lists, obsidian_writer
        paths = obsidian_writer.write_90_auto(auto_lists.generate_all())
        out["auto_lists"] = [str(p) for p in paths]
    except Exception as exc:
        out["auto_lists"] = f"failed: {type(exc).__name__}: {exc}"
        out["failed"].append("auto_lists")

    typer.echo(json.dumps(out, ensure_ascii=False, indent=2))
    if out["failed"]:
        raise typer.Exit(code=1)


@review_app.command("weekly")
def review_weekly(
    week: str | None = typer.Option(
        None, "--week", help="ISO week (e.g. 2099-W01). Defaults to the current week.",
    ),
) -> None:
    """Generate the weekly review into 40 Reviews/Weekly (M4, DESIGN.md §5.4).

    Exit 0 both on creation and when the week's file already exists (the
    launchd login trigger re-runs this; an existing week must stay quiet).
    A failed AI draft is reported in the JSON but does not fail the run (S4).
    """
    from .deliver import weekly_review
    try:
        out = weekly_review.run(week=week)
    except Exception as exc:
        typer.echo(f"review weekly failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(json.dumps(out, ensure_ascii=False, indent=2))


@index_app.command("rebuild")
def index_rebuild() -> None:
    """Rebuild derived data from the originals (M2, DESIGN.md §5.7).

    Fills gaps rather than nuking: rechunk skips messages/items already at
    the current chunking version (so existing embeddings survive — a forced
    full rechunk would CASCADE-delete every embedding and cost hours of
    re-embedding), then re-embeds only missing chunks, rebuilds both FTS
    indexes, the vec0 mirror, and item_links. Embedding is skipped with a
    note when no provider/model is available.
    """
    from . import db

    item_chunks = db.rechunk_items(force=False)
    item_chunks.pop("chunk_ids", None)  # internal detail; huge in real runs
    out: dict = {
        "chunks_messages": db.rechunk_messages(force=False),
        "chunks_items": item_chunks,
    }
    conn = db.connect()
    with conn:
        # messages_fts is external-content: 'rebuild' re-reads from messages.
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        # chunks_fts is standalone AND partial (item_text only) — 'rebuild'
        # doesn't apply; delete + reinsert the indexed subset instead.
        conn.execute("DELETE FROM chunks_fts")
        conn.execute(
            "INSERT INTO chunks_fts(rowid, text)"
            " SELECT id, text FROM chunks WHERE kind='item_text'"
        )
    try:
        provider = db._active_embedding_provider()
        out["embeddings"] = db.embed_chunks(provider, only_missing=True)
    except Exception as exc:
        out["embeddings"] = f"skipped: {exc}"
    idx = db.vector_index()
    out["vector_index"] = {"backend": idx.name, "vectors": idx.rebuild(conn)}
    out["item_links"] = db.rebuild_item_links()
    typer.echo(json.dumps(out, ensure_ascii=False, indent=2))


def main() -> None:
    """Run the CLI, notifying on failure when launched by an alerting agent.

    Typer/Click raise ``SystemExit`` with the process exit code (0 on success,
    1 on a command's ``typer.Exit(1)`` or an unhandled error). When this run
    was started by a launchd agent (``CAIRN_NOTIFY`` truthy) and the code is
    non-zero, post a macOS failure notification (M6/S4). Notification is
    best-effort and never changes the exit code the agent sees."""
    from . import ops

    try:
        app()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if not exc.code else 1)
        if code and ops.should_notify():
            ops.notify_failure(code)
        raise


if __name__ == "__main__":
    main()
