"""H0/H1 CLI: init / doctor / import / status / report via the real
`cairn` entry point (app.cli wiring included)."""
from __future__ import annotations

import json

from typer.testing import CliRunner

from app.cli import app

runner = CliRunner()


def _json(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_init_and_doctor(health_home):
    from app.health import schema

    out = _json(runner.invoke(app, ["health", "init"]))
    assert out["schema_version"] == schema.SCHEMA_VERSION
    assert set(out["subdirs"]) == {"raw", "store", "derived", "reports",
                                   "quarantine", "backups"}
    doc = _json(runner.invoke(app, ["health", "doctor"]))
    assert doc["ok"] is True


def test_doctor_fails_before_init(health_home):
    result = runner.invoke(app, ["health", "doctor"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False


def test_import_status_report_flow(health_home, catalog_dir, labs_csv_path,
                                   monkeypatch):
    # The CLI uses the packaged catalog; point the importer at the synthetic
    # one for the test (the CLI surface itself is what's under test here).
    from app.health import catalog as catalog_mod
    packaged_load = catalog_mod.load
    monkeypatch.setattr(catalog_mod, "load",
                        lambda d=None: packaged_load(d or catalog_dir))

    _json(runner.invoke(app, ["health", "init"]))
    out = _json(runner.invoke(app, ["health", "import", "labs-csv",
                                    str(labs_csv_path)]))
    assert out["inserted"] == 10
    # stdout carries counts/ids only — no values or absolute paths.
    assert "1.23" not in json.dumps(out)
    assert str(labs_csv_path) not in json.dumps(out)

    status = _json(runner.invoke(app, ["health", "status"]))
    assert status["observations"] == 10
    assert status["last_import_status"] in ("ok", "partial")

    report = _json(runner.invoke(app, ["health", "report", "labs"]))
    assert report["path"] == "reports/lab-summary.md"


def test_import_failure_exits_nonzero(health_home, tmp_path):
    missing = tmp_path / "nope.csv"
    result = runner.invoke(app, ["health", "import", "labs-csv", str(missing)])
    assert result.exit_code == 1
