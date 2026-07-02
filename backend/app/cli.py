"""`cairn` CLI (M0 skeleton, DESIGN.md §5.7).

Three top-level command groups: `sync`, `review`, `index`. At M0 only
`sync conversations` is wired — it calls into cli_sync.scan_once() which is
already Phase-1 code. The other subcommands print a milestone-specific
"not yet implemented" message and exit 1 so scripting mistakes fail loud
rather than pretending to succeed.

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
    help="Cairn integration CLI (M0 skeleton).",
)

sync_app = typer.Typer(
    no_args_is_help=True,
    help="Pull data into Cairn. Only `conversations` is wired at M0.",
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


@sync_app.command("karakeep")
def sync_karakeep() -> None:
    """(Stub) Pull Karakeep bookmarks — implemented in M1."""
    _todo("sync karakeep", "M1")


@sync_app.command("zotero")
def sync_zotero() -> None:
    """(Stub) Pull Zotero bibliography — implemented in M1."""
    _todo("sync zotero", "M1")


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
