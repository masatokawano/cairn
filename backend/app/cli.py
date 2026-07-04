"""`cairn` CLI (DESIGN.md §5.7).

Three top-level command groups: `sync`, `review`, `index`. Wired so far:
`sync conversations` (M0, Phase-1 cli_sync.scan_once) and
`sync karakeep` / `sync zotero` (M1 connectors). The remaining subcommands
print a milestone-specific "not yet implemented" message and exit 1 so
scripting mistakes fail loud rather than pretending to succeed.

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


def _todo(where: str, milestone: str) -> None:
    """Print a milestone note and exit 1 so unimplemented paths fail loud."""
    typer.echo(f"{where}: not implemented yet — landing in {milestone}.", err=True)
    raise typer.Exit(code=1)


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
    """(Stub) Index the Obsidian vault — implemented in M3."""
    _todo("sync obsidian", "M3")


@sync_app.command("all")
def sync_all() -> None:
    """(Stub) Sync every source in one pass — wired once M1 and M3 land."""
    _todo("sync all", "M3 (needs M1 first)")


@review_app.command("weekly")
def review_weekly(
    week: str | None = typer.Option(
        None, "--week", help="ISO week (e.g. 2099-W01). Defaults to the current week.",
    ),
) -> None:
    """(Stub) Generate the weekly review — implemented in M4."""
    _todo("review weekly", "M4")


@index_app.command("rebuild")
def index_rebuild() -> None:
    """(Stub) Rebuild all derived data — implemented in M2.

    Note: embedding regeneration alone is available today via
    `python -m app.admin reindex`. That path stays for M0.
    """
    typer.echo(
        "index rebuild: not implemented yet — landing in M2.\n"
        "For embedding regeneration alone, use `python -m app.admin reindex`.",
        err=True,
    )
    raise typer.Exit(code=1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
