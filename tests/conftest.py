"""Test isolation.

Integration tests must never touch the development database or the eval truth files in
data/: `make data-build` output there is the dataset the models are trained and scored on,
and a stray `build(n_persons=200, seed=...)` from a test would overwrite it.

  - POSTGRES_DB is redirected to `continuum_test` *before* anything imports continuum (so
    the cached Settings and the SQLAlchemy engine both see it).
  - A session fixture drops/recreates that database and applies the migrations, but only if
    an integration test was collected.
  - Output paths and synthetic sanctions generation in continuum.foundation.build are
    redirected/disabled per test.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ["POSTGRES_DB"] = "continuum_test"

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from continuum.config.settings import get_settings

REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture(scope="session", autouse=True)
def _test_database(request: pytest.FixtureRequest) -> None:
    if not any("integration" in item.keywords for item in request.session.items):
        return
    s = get_settings()
    assert s.postgres_db == "continuum_test"
    with psycopg.connect(
        host=s.postgres_host,
        port=s.postgres_port,
        user=s.postgres_user,
        password=s.postgres_password,
        dbname="postgres",
        autocommit=True,
    ) as conn:
        conn.execute("DROP DATABASE IF EXISTS continuum_test WITH (FORCE)")
        conn.execute("CREATE DATABASE continuum_test")
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "continuum" / "db" / "migrations"))
    command.upgrade(cfg, "head")


@pytest.fixture(autouse=True)
def _isolate_build_outputs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from continuum.foundation import build

    monkeypatch.setattr(build, "SANCTIONS_CSV", tmp_path / "targets.simple.csv")
    monkeypatch.setattr(build, "SANCTIONS_PLANTED_CSV", tmp_path / "sanctions_planted.csv")
    monkeypatch.setattr(build, "TRUE_UPLIFT_CSV", tmp_path / "true_uplift.csv")
    # No sanctions file -> the loader skips the snapshot instead of generating one.
    monkeypatch.setattr(build.synthetic_sanctions, "write_synthetic_sanctions", lambda *a, **k: 0)
