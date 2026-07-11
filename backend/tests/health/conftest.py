"""Health-domain test fixtures. Synthetic data ONLY (AGENTS.md invariant 9,
ACCEPTANCE.md §0): metric names, values, dates and reference ranges below
are invented and must never be replaced with real personal data.
"""
from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def health_home(tmp_path, monkeypatch) -> Path:
    """Point CAIRN_HEALTH_HOME at a per-test temporary directory."""
    home = tmp_path / "health-home"
    monkeypatch.setenv("CAIRN_HEALTH_HOME", str(home))
    return home


@pytest.fixture
def catalog_dir() -> Path:
    return FIXTURES / "catalog"


@pytest.fixture
def labs_csv_path() -> Path:
    return FIXTURES / "synthetic_labs.csv"


@pytest.fixture
def imported(health_home, catalog_dir, labs_csv_path):
    """Import the synthetic fixture once; returns (home, stats)."""
    from app.health.importers import labs_csv

    stats = labs_csv.run(labs_csv_path, catalog_dir=catalog_dir)
    return health_home, stats
