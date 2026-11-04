"""Shared pytest fixtures.

Integration/e2e tests use a REAL PostgreSQL (`LAE_TEST_DATABASE_URL`, default: the docker-compose
`lae_test` database on localhost:5433). If unreachable, those tests are SKIPPED with an explicit
reason. They never silently pass.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

DEFAULT_TEST_URL = "postgresql+psycopg://lae_owner:lae_owner_dev@127.0.0.1:5433/lae_test"


def test_database_url() -> str:
    return os.environ.get("LAE_TEST_DATABASE_URL", DEFAULT_TEST_URL)


@pytest.fixture(scope="session")
def pg_url() -> str:
    return test_database_url()


@pytest.fixture(scope="session")
def pg_engine(pg_url: str) -> Iterator[object]:
    """Session-wide SQLAlchemy engine on the test database (skips if PostgreSQL is down)."""
    sqlalchemy = pytest.importorskip("sqlalchemy")
    engine = sqlalchemy.create_engine(pg_url, future=True, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            conn.execute(sqlalchemy.text("SELECT 1"))
    except Exception as exc:
        engine.dispose()
        pytest.skip(f"PostgreSQL test database unavailable at {pg_url.split('@')[-1]}: {exc}")
    yield engine
    engine.dispose()
