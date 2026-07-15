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
                broken = _broken_refs(home)
                check("provenance_intact", not broken,
                      f"{len(broken)} broken source/text reference(s)"
                      if broken else "")
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
        committable = result["tracked"] + result["untracked"]
        if committable:
            check("repo_audit", False, "committable health artifacts: "
                  + json.dumps(committable, ensure_ascii=False))
        elif result["ignored"]:
            # Gitignored (can't be committed) but PRIVACY §3 wants it out of
            # the worktree — warn without failing the gate.
            check("repo_audit", True, f"clean for commit; WARNING: "
                  f"{len(result['ignored'])} gitignored health file(s) in the "
                  "worktree — move real data to the health data home")
        else:
            check("repo_audit", True, "clean")

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


def _broken_refs(home) -> list:
    from . import analytics, store

    conn = store.connect(home)
    try:
        return analytics.broken_references(conn, home)
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


@import_app.command("apple-export")
def import_apple_export(
    file: Path = typer.Argument(..., help="Apple Health export.zip or export.xml."),
) -> None:
    """Import allowlisted Apple Health record types (H3, streaming)."""
    from .importers import apple_health

    try:
        stats = apple_health.run(file)
    except apple_health.AppleHealthError as exc:
        typer.echo(f"import failed: {exc}", err=True)
        raise typer.Exit(code=1)
    except Exception as exc:
        typer.echo(f"import failed: {type(exc).__name__}", err=True)
        raise typer.Exit(code=1)
    _echo(stats)


@import_app.command("document")
def import_document(
    file: Path = typer.Argument(..., help="Medical document file (PDF/image/…)."),
    kind: str = typer.Option(..., "--kind",
                             help="lab_report / imaging / endoscopy / prescription / …"),
    title: str | None = typer.Option(None, "--title"),
    date: str | None = typer.Option(None, "--date", help="Document date YYYY-MM-DD."),
    issuer: str | None = typer.Option(None, "--issuer", help="Issuing facility."),
) -> None:
    """Register a medical document (immutable snapshot, no auto-extraction)."""
    from .importers import documents

    try:
        stats = documents.register(file, kind=kind, title=title,
                                   document_date=date, issuer=issuer)
    except documents.DocumentError as exc:
        typer.echo(f"register failed: {exc}", err=True)
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


backup_app = typer.Typer(no_args_is_help=True,
                         help="Backup / restore / retention (H8).")
health_app.add_typer(backup_app, name="backup")


@backup_app.command("create")
def backup_create(
    dest: Path | None = typer.Option(
        None, "--dest", help="Destination dir (default: <home>/backups). Must "
        "be outside the git worktree; copy off-machine to encrypted storage."),
) -> None:
    """Write a consistent .tar.gz snapshot (store + raw + reports + manifest)."""
    from . import ops

    try:
        _echo(ops.backup(dest_dir=dest))
    except ops.OpsError as exc:
        typer.echo(f"backup failed: {exc}", err=True)
        raise typer.Exit(code=1)


@backup_app.command("list")
def backup_list(
    dest: Path | None = typer.Option(None, "--dest"),
) -> None:
    """List backup archives."""
    from . import ops

    _echo({"backups": ops.list_backups(dest)})


@backup_app.command("verify")
def backup_verify(archive: Path) -> None:
    """Check an archive's store hash against its manifest."""
    from . import ops

    out = ops.verify_backup(archive)
    _echo(out)
    if not out["ok"]:
        raise typer.Exit(code=1)


@backup_app.command("restore")
def backup_restore(
    archive: Path,
    into: Path = typer.Option(..., "--into",
                              help="Empty target home directory."),
) -> None:
    """Restore an archive into an empty home and verify counts + hashes."""
    from . import ops

    try:
        out = ops.restore(archive, into)
    except ops.OpsError as exc:
        typer.echo(f"restore failed: {exc}", err=True)
        raise typer.Exit(code=1)
    _echo(out)
    if not out["ok"]:
        raise typer.Exit(code=1)


@backup_app.command("rotate")
def backup_rotate(
    keep: int = typer.Option(7, "--keep", help="Newest N archives to keep."),
    dest: Path | None = typer.Option(None, "--dest"),
) -> None:
    """Delete backup archives older than the newest --keep (backups only)."""
    from . import ops

    try:
        _echo(ops.rotate_backups(dest, keep=keep))
    except ops.OpsError as exc:
        typer.echo(f"rotate failed: {exc}", err=True)
        raise typer.Exit(code=1)


@health_app.command("delete-derived")
def delete_derived() -> None:
    """Delete regenerable derived data + reports (sources/store untouched)."""
    from . import ops

    _echo(ops.delete_derived())


@health_app.command("purge")
def purge(
    confirm: bool = typer.Option(
        False, "--yes-delete-everything",
        help="Required. Irreversibly deletes the ENTIRE health data home."),
) -> None:
    """DESTRUCTIVE: delete the entire health data home. Lists everything first
    and requires the explicit flag (AGENTS.md invariant 8)."""
    from . import config, ops

    home = config.resolve_home()
    plan = ops.purge_plan(home)
    _echo({"will_delete": plan})
    if not confirm:
        typer.echo("refused: re-run with --yes-delete-everything to proceed",
                   err=True)
        raise typer.Exit(code=1)
    _echo(ops.purge(home, confirm=str(home)))


document_app = typer.Typer(no_args_is_help=True,
                           help="Manage registered medical documents (H4).")
health_app.add_typer(document_app, name="document")


@document_app.command("attach-text")
def document_attach_text(
    document_id: str = typer.Argument(..., help="Document id from `import document`."),
    text_file: Path = typer.Argument(..., help="Extracted/transcribed text file."),
    verified: bool = typer.Option(
        False, "--verified",
        help="Mark as human-verified. Without this the text is stored as draft "
             "(OCR/extracted text is never silently trusted).",
    ),
) -> None:
    """Attach extracted text to a document (status draft, or verified)."""
    from .importers import documents

    try:
        _echo(documents.attach_text(document_id, text_file, verified=verified))
    except documents.DocumentError as exc:
        typer.echo(f"attach failed: {exc}", err=True)
        raise typer.Exit(code=1)


@document_app.command("list")
def document_list() -> None:
    """List registered documents (metadata only, no extracted text)."""
    from . import analytics, config, store

    conn = store.connect(config.resolve_home())
    try:
        _echo({"documents": analytics.documents(conn)})
    finally:
        conn.close()


interpret_app = typer.Typer(
    no_args_is_help=True,
    help="Interpretations with evidence and revision history (H6). "
         "AI output is a draft, never an authority — accept/reject is yours.",
)
health_app.add_typer(interpret_app, name="interpret")


def _conn():
    from . import config, store

    return store.connect(config.resolve_home())


@interpret_app.command("draft-ai")
def interpret_draft_ai(
    metric: list[str] = typer.Option(..., "--metric", "-m",
                                     help="Canonical metric id (repeatable, max 8)."),
    since: str | None = typer.Option(None, "--since", help="YYYY-MM-DD"),
    until: str | None = typer.Option(None, "--until", help="YYYY-MM-DD"),
    question: str | None = typer.Option(None, "--question", "-q"),
    max_rows: int = typer.Option(50, "--max-rows",
                                 help="Bounded model context (observations)."),
    deliver: bool = typer.Option(
        False, "--deliver",
        help="Also write the draft into the vault's 00 Inbox/AI Drafts "
             "(new file only). WARNING: AI Drafts IS synced to other "
             "devices — unlike 90 Auto/Health it is not excluded.",
    ),
) -> None:
    """Generate an AI interpretation draft (local ollama, full provenance)."""
    from . import interpret

    conn = _conn()
    try:
        out = interpret.ai_draft(conn, metrics=metric, since=since,
                                 until=until, question=question,
                                 max_rows=max_rows)
        if deliver:
            row = conn.execute(
                "SELECT title, body_markdown, author_label, limitations,"
                " confidence FROM interpretations WHERE id=?",
                [out["interpretation_id"]]).fetchone()
            from ..deliver import obsidian_writer
            from ..deliver.auto_lists import _esc
            # Provenance in the regulated form cairn/<model>/<prompt_version>
            # (DESIGN §6.2, AGENTS 不変条件4) — same shape as weekly drafts.
            provenance = f"cairn/{out['model']}/{out['prompt_version']}"
            # title / limitations are prose positions → escape untrusted LLM
            # text so injected markdown can't build links/embeds there.
            # body_markdown is intentionally rendered AS markdown (owner
            # decision 2026-07-15; documented trust boundary in PRIVACY §7):
            # it is locally-generated, opt-in delivered, and needs formatting.
            md = (f"# {_esc(row[0])}\n\n"
                  f"- generated_by: {provenance}\n"
                  f"- draft — 採否は人間\n"
                  f"- confidence: {row[4]}\n\n"
                  f"{row[1]}\n\n"
                  f"## Limitations\n\n{_esc(row[3])}\n")
            path = obsidian_writer.write(
                "draft", f"health-interpretation-{out['interpretation_id'][:8]}.md", md)
            out["delivered_to"] = str(path)
            typer.echo("note: AI Drafts は他端末へ同期されます（--deliver は明示 opt-in）",
                       err=True)
    except interpret.SafetyError as exc:
        typer.echo(f"draft rejected by safety gate: {exc}", err=True)
        raise typer.Exit(code=1)
    except Exception as exc:
        # Only the exception TYPE — a provider error message can echo raw
        # model output (health content). Details stay in the run record.
        typer.echo(f"draft failed: {type(exc).__name__}", err=True)
        raise typer.Exit(code=1)
    finally:
        conn.close()
    _echo(out)


@interpret_app.command("add")
def interpret_add(
    title: str = typer.Option(..., "--title"),
    body_file: Path = typer.Option(..., "--body-file",
                                   help="Markdown file with the interpretation body."),
    author: str = typer.Option("self", "--author", help="self / clinician"),
    author_label: str | None = typer.Option(None, "--author-label",
                                            help="Clinician name etc."),
    evidence: list[str] = typer.Option(
        [], "--evidence", "-e",
        help="kind:id[:role] (kind=observation/event/document/reference,"
             " role defaults to supports). Repeatable."),
    supersedes: str | None = typer.Option(None, "--supersedes"),
) -> None:
    """Record a human interpretation (yours or a clinician's explanation)."""
    from . import interpret

    parsed = []
    for item in evidence:
        parts = item.split(":")
        if len(parts) == 2:
            parsed.append((parts[0], parts[1], "supports"))
        elif len(parts) == 3:
            parsed.append((parts[0], parts[1], parts[2]))
        else:
            typer.echo(f"bad --evidence {item!r} (kind:id[:role])", err=True)
            raise typer.Exit(code=1)
    conn = _conn()
    try:
        interp_id = interpret.add(
            conn, author_type=author, author_label=author_label or author,
            title=title, body_markdown=body_file.read_text("utf-8"),
            evidence=parsed, supersedes=supersedes)
    except interpret.InterpretError as exc:
        typer.echo(f"add failed: {exc}", err=True)
        raise typer.Exit(code=1)
    finally:
        conn.close()
    _echo({"interpretation_id": interp_id, "status": "draft"})


@interpret_app.command("accept")
def interpret_accept(interp_id: str) -> None:
    """Accept a draft (human decision; requires at least one evidence row)."""
    from . import interpret

    conn = _conn()
    try:
        interpret.set_status(conn, interp_id, "accepted")
    except interpret.InterpretError as exc:
        typer.echo(f"accept failed: {exc}", err=True)
        raise typer.Exit(code=1)
    finally:
        conn.close()
    _echo({"interpretation_id": interp_id, "status": "accepted"})


@interpret_app.command("reject")
def interpret_reject(interp_id: str) -> None:
    """Reject a draft (kept forever — rejected interpretations are the 供養録)."""
    from . import interpret

    conn = _conn()
    try:
        interpret.set_status(conn, interp_id, "rejected")
    finally:
        conn.close()
    _echo({"interpretation_id": interp_id, "status": "rejected"})


@interpret_app.command("list")
def interpret_list(
    status: list[str] = typer.Option(
        [], "--status", help="Filter (repeatable). "
        "--status rejected --status superseded = 供養録."),
) -> None:
    """List interpretations (metadata only, bodies stay in the store)."""
    from . import interpret

    conn = _conn()
    try:
        _echo({"interpretations": interpret.listing(conn, status or None)})
    finally:
        conn.close()


report_app = typer.Typer(no_args_is_help=True, help="Generate factual reports.")
health_app.add_typer(report_app, name="report")


@report_app.command("labs")
def report_labs() -> None:
    """Write the deterministic factual lab summary into reports/."""
    from .reports import lab_summary

    _echo(lab_summary.write())


@report_app.command("data-quality")
def report_data_quality() -> None:
    """Per-metric coverage and quality (counts only, no values)."""
    from . import analytics, config, store

    conn = store.connect(config.resolve_home())
    try:
        _echo({"metrics": analytics.data_quality(conn)})
    finally:
        conn.close()


@health_app.command("deliver")
def deliver() -> None:
    """Write the four factual reports into the vault's 90 Auto/Health (H5).

    The folder is excluded from vault sync by default (PRIVACY.md H5-P1) —
    reports exist only on this Mac unless a type is explicitly opted in."""
    from .reports import vault_reports

    try:
        _echo(vault_reports.deliver())
    except Exception as exc:
        typer.echo(f"deliver failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1)


@report_app.command("broken-refs")
def report_broken_refs() -> None:
    """List rows whose source snapshot or extracted text is missing on disk."""
    from . import analytics, config, store

    home = config.resolve_home()
    conn = store.connect(home)
    try:
        broken = analytics.broken_references(conn, home)
    finally:
        conn.close()
    _echo({"ok": not broken, "broken": broken})
    if broken:
        raise typer.Exit(code=1)


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
