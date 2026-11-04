"""Integration fixtures (owned by the Database Engineer). REAL PostgreSQL, `lae_test` ONLY.

Fixtures for every integration test:

* ``migrated_db`` (session): drops and recreates schemas ``lae`` and ``app`` in ``lae_test``,
  runs ``alembic upgrade head`` and returns a SQLAlchemy ``Engine``. Refuses any other database.
* ``seeded_db`` (function): ``(engine, ground_truth)`` with the demo dataset loaded
  (default ``scale="tiny", seed=7``). Re-seeds only when the requested (scale, seed) differs
  from what is loaded. Override with indirect parametrization:
  ``@pytest.mark.parametrize("seeded_db", [{"scale": "small", "seed": 1}], indirect=True)``.
  Tests must NOT commit changes to ``app``; use ``db_conn`` / ``db_session`` (rolled back), or
  call ``mark_dataset_dirty`` after committing so the next test re-seeds.
* ``db_conn``: a ``Connection`` inside a transaction that is always rolled back.
* ``db_session``: a ``Session`` on ``db_conn`` (``session.commit()`` only releases a savepoint).
* ``app_conn``: like ``db_conn`` but guaranteed to run after ``seeded_db``.
* ``generator``: the ``db/fixtures/generator.py`` module.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
GENERATOR_PATH = REPO_ROOT / "db" / "fixtures" / "generator.py"
ALLOWED_TEST_DATABASE = "lae_test"
DEFAULT_SCALE = "tiny"
DEFAULT_SEED = 7


def _psycopg_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def load_generator() -> ModuleType:
    name = "lae_fixtures_generator"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, GENERATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses need the module registered before exec
    spec.loader.exec_module(module)
    return module


def run_alembic(url: str, revision: str = "head", *, downgrade: bool = False) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.attributes["configure_logger"] = False
    if downgrade:
        command.downgrade(cfg, revision)
    else:
        command.upgrade(cfg, revision)


RESET_SQL = (
    "DROP SCHEMA IF EXISTS app CASCADE",
    "DROP SCHEMA IF EXISTS lae CASCADE",
    "CREATE EXTENSION IF NOT EXISTS pgcrypto",
    "CREATE EXTENSION IF NOT EXISTS vector",
    "CREATE SCHEMA lae",
    "CREATE SCHEMA app",
    """DO $$ BEGIN
         IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lae_reader') THEN
           GRANT USAGE ON SCHEMA lae, app TO lae_reader;
           ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT SELECT ON TABLES TO lae_reader;
           ALTER DEFAULT PRIVILEGES IN SCHEMA lae GRANT SELECT ON TABLES TO lae_reader;
         END IF;
       END $$""",
)


@pytest.fixture(scope="session")
def migrated_url(pg_url: str) -> str:
    url = _psycopg_url(pg_url)
    if make_url(url).database != ALLOWED_TEST_DATABASE:
        pytest.fail(
            f"integration fixtures only run against the {ALLOWED_TEST_DATABASE!r} database",
            pytrace=False,
        )
    return url


@pytest.fixture(scope="session")
def migrated_db(migrated_url: str) -> Iterator[Engine]:
    engine = create_engine(migrated_url, future=True, pool_pre_ping=True, hide_parameters=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        engine.dispose()
        pytest.skip(f"PostgreSQL test database unavailable: {type(exc).__name__}")
    with engine.begin() as conn:
        for stmt in RESET_SQL:
            conn.execute(text(stmt))
    run_alembic(migrated_url)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def generator() -> ModuleType:
    return load_generator()


@pytest.fixture(scope="session")
def _dataset_state() -> dict[str, Any]:
    return {"key": None, "truth": None}


@pytest.fixture
def mark_dataset_dirty(_dataset_state: dict[str, Any]) -> Any:
    def _mark() -> None:
        _dataset_state["key"] = None
        _dataset_state["truth"] = None

    return _mark


@pytest.fixture
def seeded_db(
    request: pytest.FixtureRequest,
    migrated_db: Engine,
    generator: ModuleType,
    _dataset_state: dict[str, Any],
) -> tuple[Engine, Any]:
    params = getattr(request, "param", None) or {}
    scale = params.get("scale", DEFAULT_SCALE)
    seed = int(params.get("seed", DEFAULT_SEED))
    key = (scale, seed)
    if _dataset_state["key"] != key:
        _dataset_state["key"] = None
        truth = generator.seed_database(migrated_db, scale, seed)
        _dataset_state.update(key=key, truth=truth)
    return migrated_db, _dataset_state["truth"]


@pytest.fixture
def db_conn(migrated_db: Engine) -> Iterator[Connection]:
    conn = migrated_db.connect()
    trans = conn.begin()
    try:
        yield conn
    finally:
        if trans.is_active:
            trans.rollback()
        conn.close()


@pytest.fixture
def db_session(db_conn: Connection) -> Iterator[Session]:
    session = Session(bind=db_conn, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def app_conn(seeded_db: tuple[Engine, Any]) -> Iterator[Connection]:
    """A rolled-back Connection on the seeded dataset (use for anything that writes `app`)."""
    engine, _truth = seeded_db
    conn = engine.connect()
    trans = conn.begin()
    try:
        yield conn
    finally:
        if trans.is_active:
            trans.rollback()
        conn.close()


@pytest.fixture(scope="session")
def alembic_runner() -> Any:
    """`run_alembic(url, revision="head", *, downgrade=False)`."""
    return run_alembic
