"""Idempotently create the LAE test databases on an EXISTING PostgreSQL volume.

    python scripts/create_test_dbs.py                       # lae_test + every default suffix
    python scripts/create_test_dbs.py --names qa,bench      # lae_test_qa, lae_test_bench only
    python scripts/create_test_dbs.py --admin-url postgresql://lae_owner:...@127.0.0.1:5433/postgres

db/init/01_init.sql does the same on a FRESH volume (docker-entrypoint-initdb.d runs only once).
For each database: CREATE DATABASE (if missing), extensions pgcrypto + vector, schemas lae + app,
USAGE/SELECT grants for lae_reader and DML grants on app for lae_executor (roles are created if
missing; their passwords come from LAE_READER_PASSWORD / LAE_EXECUTOR_PASSWORD, dev defaults
otherwise). Only names `lae_test` / `lae_test_<suffix>` are accepted. Nothing is dropped.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Any

import psycopg
from psycopg import sql

DEFAULT_SUFFIXES = ("qa", "bench", "disc", "erasure", "lead", "verif")
NAME_RE = re.compile(r"^lae_test(_[a-z0-9]+)*$")
DEFAULT_ADMIN = "postgresql://lae_owner:lae_owner_dev@127.0.0.1:5433/postgres"


def _admin_url() -> str:
    url = os.environ.get("LAE_ADMIN_URL")
    if url:
        return url
    db = os.environ.get("LAE_DATABASE_URL")
    if db:  # same server and credentials, maintenance database `postgres`
        return re.sub(r"/[^/?]+(\?|$)", r"/postgres\1", db.replace("+psycopg", "", 1), count=1)
    return DEFAULT_ADMIN


def _with_db(url: str, name: str) -> str:
    return re.sub(r"/[^/?]+(\?|$)", rf"/{name}\1", url, count=1)


def ensure_roles(conn: psycopg.Connection[Any]) -> None:
    roles = {
        "lae_reader": os.environ.get("LAE_READER_PASSWORD", "lae_reader_dev"),
        "lae_executor": os.environ.get("LAE_EXECUTOR_PASSWORD", "lae_executor_dev"),
        "lae_app": os.environ.get("LAE_APP_PASSWORD", "lae_app_dev"),  # grants: migration 0002
    }
    for role, password in roles.items():
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        if not exists:
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE"
                ).format(sql.Identifier(role), sql.Literal(password))
            )
            print(f"created role {role}")


def ensure_database(admin: psycopg.Connection[Any], name: str) -> bool:
    if not NAME_RE.match(name):
        raise SystemExit(f"refusing to create {name!r}: only lae_test / lae_test_<suffix>")
    if admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
        return False
    owner = admin.execute("SELECT current_user").fetchone()
    admin.execute(
        sql.SQL("CREATE DATABASE {} OWNER {}").format(
            sql.Identifier(name),
            sql.Identifier(str(owner[0] if owner else "lae_owner")),
        )
    )
    return True


SETUP = (
    "CREATE EXTENSION IF NOT EXISTS pgcrypto",
    "CREATE EXTENSION IF NOT EXISTS vector",
    "CREATE SCHEMA IF NOT EXISTS lae",
    "CREATE SCHEMA IF NOT EXISTS app",
    "GRANT USAGE ON SCHEMA lae, app TO lae_reader, lae_executor",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT SELECT ON TABLES TO lae_reader",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA lae GRANT SELECT ON TABLES TO lae_reader",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA app "
    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lae_executor",
)


def setup_database(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        for stmt in SETUP:
            conn.execute(stmt)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--admin-url", default=None, help="URL of the maintenance DB (postgres)")
    ap.add_argument("--names", default=None, help="comma-separated suffixes (default: all)")
    ap.add_argument("--no-base", action="store_true", help="do not create plain lae_test")
    args = ap.parse_args(argv)
    admin_url = args.admin_url or _admin_url()
    suffixes = (
        [s.strip() for s in args.names.split(",") if s.strip()] if args.names else DEFAULT_SUFFIXES
    )
    names = ([] if args.no_base or args.names else ["lae_test"]) + [
        f"lae_test_{s}" for s in suffixes
    ]
    try:
        admin = psycopg.connect(admin_url, autocommit=True)
    except psycopg.OperationalError as exc:
        print(f"cannot connect to PostgreSQL: {type(exc).__name__}", file=sys.stderr)
        return 2
    with admin:
        ensure_roles(admin)
        for name in names:
            created = ensure_database(admin, name)
            setup_database(_with_db(admin_url, name))
            print(f"{name}: {'created' if created else 'exists'}; extensions/schemas/grants ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
