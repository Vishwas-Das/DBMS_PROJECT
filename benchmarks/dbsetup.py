"""Benchmark database lifecycle: guard, reset, migrate, seed. Only `lae_test_*` databases."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import psycopg
from sqlalchemy.engine import make_url

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
log = logging.getLogger("benchmarks.db")

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
         END IF;
       END $$""",
)


def sa_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def pg_url(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def database_name(url: str) -> str:
    return make_url(sa_url(url)).database or ""


def guard_bench_url(url: str) -> None:
    """Destructive resets are only ever allowed on a dedicated `lae_test_<suffix>` database."""
    name = database_name(url)
    if not name.startswith("lae_test_"):
        raise SystemExit(f"refusing to reset database {name!r}: benchmarks need lae_test_<suffix>")


def connect(url: str, *, autocommit: bool = False) -> psycopg.Connection[Any]:
    return psycopg.connect(pg_url(url), autocommit=autocommit)


def reset_and_migrate(url: str) -> None:
    guard_bench_url(url)
    with connect(url) as conn:
        for stmt in RESET_SQL:
            conn.execute(stmt)
        conn.commit()
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.set_main_option("sqlalchemy.url", sa_url(url).replace("%", "%%"))
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")


def seed(url: str, scale: str, seed_value: int) -> Any:
    """Deterministic dataset + ground truth (db/fixtures/generator.py)."""
    guard_bench_url(url)
    from db.fixtures import generator

    return generator.seed_database(pg_url(url), scale, seed_value)


def vacuum_analyze(url: str) -> None:
    with connect(url, autocommit=True) as conn:
        conn.execute("VACUUM ANALYZE")
