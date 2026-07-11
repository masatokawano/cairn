"""`cairn health` sub-CLI (H0: init/doctor, H1: import labs-csv/status/report).

Output discipline (PRIVACY.md §5): stdout may end up in launchd log files,
so commands print counts, versions, short hashes and run ids — never
measurement values, metric names, dates of real observations, or absolute
source paths. duckdb is imported lazily inside commands.
"""
from __future__ import annotations

import json
import stat
from pathlib import Path

import typer

health_app = typer.Typer(
    no_args_is_help=True,
    help="Personal Health Observatory (ADR-0005; independent store, H0/H1).",
)


def _echo(payload: dict) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


@health_app.command("init")
def init() -> None:
    """Create the protected data home and initialize the health store."""
    from . import config, store

    home = config.ensure_home()
    conn = store.connect(home, create=True)
    try:
        info = store.counts(conn)
    finally:
        conn.close()
    _echo({"home": str(home), "schema_version": info["schema_version"],
           "subdirs": list(config.SUBDIRS)})


@health_app.command("doctor")
def doctor() -> None:
    """Verify the safety boundary: paths, permissions, store, repo audit."""
    from . import audit, config, store

    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, **({"detail": detail} if detail else {})})

    try:
        home = config.resolve_home()
        check("home_resolvable", True, str(home))
    except config.HealthConfigError as exc:
        check("home_resolvable", False, str(exc))
        _echo({"ok": False, "checks": checks})
        raise typer.Exit(code=1)

    if home.exists():
        mode = stat.S_IMODE(home.stat().st_mode)
        check("home_mode_0700", mode == config.DIR_MODE, oct(mode))
        for name in config.SUBDIRS:
            sub = home / name
            if not sub.exists():
                check(f"subdir_{name}", False, "missing (run `cairn health init`)")
            elif sub.is_symlink():
                check(f"subdir_{name}", False, "symlink refused")
            else:
                sub_mode = stat.S_IMODE(sub.stat().st_mode)
                check(f"subdir_{name}", sub_mode == config.DIR_MODE, oct(sub_mode))
        store_file = config.store_path(home)
        if store_file.exists():
            file_mode = stat.S_IMODE(store_file.stat().st_mode)
            check("store_mode_0600", file_mode == config.FILE_MODE, oct(file_mode))
            try:
                conn = store.connect(home)
                info = store.counts(conn)
                conn.close()
                check("store_openable", True, f"schema v{info['schema_version']}")
                stuck = info["import_runs"] and _stuck_runs(home)
                check("no_stuck_import_runs", not stuck,
                      f"{stuck} run(s) left in 'running'" if stuck else "")
            except Exception as exc:
                check("store_openable", False, type(exc).__name__)
        else:
            check("store_openable", False, "not initialized (run `cairn health init`)")
    else:
        check("home_exists", False, "run `cairn health init`")

    result = audit.scan()
    if "skipped" in result:
        check("repo_audit", True, result["skipped"])
    else:
        hits = sum(len(result[k]) for k in ("tracked", "untracked", "ignored"))
        check("repo_audit", result["ok"],
              "clean" if result["ok"] else f"{hits} suspicious file(s): "
              + json.dumps({k: result[k] for k in ("tracked", "untracked", "ignored") if result[k]}))

    ok = all(c["ok"] for c in checks)
    _echo({"ok": ok, "checks": checks})
    if not ok:
        raise typer.Exit(code=1)


def _stuck_runs(home) -> int:
    from . import store

    conn = store.connect(home)
    try:
        return conn.execute(
            "SELECT count(*) FROM import_runs WHERE status='running'"
        ).fetchone()[0]
    finally:
        conn.close()


import_app = typer.Typer(no_args_is_help=True, help="Import health sources.")
health_app.add_typer(import_app, name="import")


@import_app.command("labs-csv")
def import_labs_csv(
    file: Path = typer.Argument(..., help="Horizontal lab CSV (dates as columns)."),
    source_name: str = typer.Option(
        "lab_sheet", "--source-name",
        help="Origin label stored on each observation (part of the fingerprint).",
    ),
) -> None:
    """Import a laboratory CSV (immutable snapshot + normalized observations)."""
    from .importers import labs_csv

    try:
        stats = labs_csv.run(file, source_name=source_name)
    except Exception as exc:
        typer.echo(f"import failed: {type(exc).__name__}", err=True)
        raise typer.Exit(code=1)
    _echo(stats)


@import_app.command("events")
def import_events(
    file: Path = typer.Argument(..., help="Event ledger YAML (append-only;"
                                          " corrections via 'supersedes')."),
) -> None:
    """Import medication/supplement/lifestyle events (H2)."""
    from .importers import events_yaml

    try:
        stats = events_yaml.run(file)
    except events_yaml.EventsError as exc:
        # EventsError messages reference entry ids only — safe to surface.
        typer.echo(f"import failed: {exc}", err=True)
        raise typer.Exit(code=1)
    except Exception as exc:
        typer.echo(f"import failed: {type(exc).__name__}", err=True)
        raise typer.Exit(code=1)
    _echo(stats)


@health_app.command("status")
def status() -> None:
    """Store row counts and versions (no values, metric names or dates)."""
    from . import config, store

    conn = store.connect(config.resolve_home())
    try:
        info = store.counts(conn)
        last = conn.execute(
            "SELECT status FROM import_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    info["last_import_status"] = last[0] if last else None
    _echo(info)


report_app = typer.Typer(no_args_is_help=True, help="Generate factual reports.")
health_app.add_typer(report_app, name="report")


@report_app.command("labs")
def report_labs() -> None:
    """Write the deterministic factual lab summary into reports/."""
    from .reports import lab_summary

    _echo(lab_summary.write())


@report_app.command("event-response")
def report_event_response(
    event_id: str = typer.Argument(..., help="Event id from the ledger."),
    days: int = typer.Option(90, "--days",
                             help="Window size around the event start."),
) -> None:
    """Write a factual before/after window report for one event (H2)."""
    from .reports import event_response

    try:
        _echo(event_response.write(event_id, window_days=days))
    except KeyError as exc:
        typer.echo(f"report failed: {exc}", err=True)
        raise typer.Exit(code=1)
