"""Alembic baseline: round trip, metadata parity, triggers, grants."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, Engine, text

from lae.db.models import Base

pytestmark = pytest.mark.integration

LAE_TABLES = {t.name for t in Base.metadata.sorted_tables}
PARITY_SCHEMA = "lae_parity"


def _tables(conn: Connection, schema: str) -> set[str]:
    rows = conn.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = :s"), {"s": schema}
    )
    return {r[0] for r in rows}


def _include_name(name: str | None, type_: str, _parents: Any) -> bool:
    return name == "lae" if type_ == "schema" else True


def _include_object(_obj: Any, name: str | None, type_: str, _r: bool, _c: Any) -> bool:
    return not (type_ == "table" and name == "alembic_version")


def test_baseline_creates_every_table(migrated_db: Engine) -> None:
    with migrated_db.connect() as conn:
        assert _tables(conn, "lae") == LAE_TABLES | {"alembic_version"}
        version = conn.execute(text("SELECT version_num FROM lae.alembic_version")).scalar_one()
    assert version == "0001_baseline"
    assert len(LAE_TABLES) == 22


def test_metadata_matches_live_schema(migrated_db: Engine) -> None:
    with migrated_db.connect() as conn:
        ctx = MigrationContext.configure(
            conn,
            opts={
                "include_schemas": True,
                "include_name": _include_name,
                "include_object": _include_object,
                "compare_type": True,
                "compare_server_default": True,
            },
        )
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == []


def _constraints(conn: Connection, schema: str) -> set[tuple[str, str, str, str]]:
    rows = conn.execute(
        text(
            "SELECT cl.relname, c.conname, c.contype, pg_get_constraintdef(c.oid, true) "
            "FROM pg_constraint c JOIN pg_class cl ON cl.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = cl.relnamespace "
            "WHERE n.nspname = :s AND cl.relname <> 'alembic_version'"
        ),
        {"s": schema},
    )
    return {(r[0], r[1], r[2], r[3].replace(f"{schema}.", "lae.")) for r in rows}


def _indexes(conn: Connection, schema: str) -> set[tuple[str, str, str]]:
    rows = conn.execute(
        text(
            "SELECT tablename, indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = :s AND tablename <> 'alembic_version'"
        ),
        {"s": schema},
    )
    return {(r[0], r[1], r[2].replace(f"{schema}.", "lae.")) for r in rows}


def _columns(conn: Connection, schema: str) -> set[tuple[Any, ...]]:
    rows = conn.execute(
        text(
            "SELECT table_name, column_name, ordinal_position, data_type, udt_name, "
            "character_maximum_length, numeric_precision, numeric_scale, is_nullable, "
            "column_default, is_identity, identity_generation "
            "FROM information_schema.columns WHERE table_schema = :s "
            "AND table_name <> 'alembic_version'"
        ),
        {"s": schema},
    )
    return {tuple(r) for r in rows}


@pytest.fixture
def parity_schema(migrated_db: Engine) -> Iterator[str]:
    """Base.metadata.create_all into a scratch schema (schema_translate_map)."""
    with migrated_db.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{PARITY_SCHEMA}" CASCADE'))
        conn.execute(text(f'CREATE SCHEMA "{PARITY_SCHEMA}"'))
        Base.metadata.create_all(
            conn.execution_options(schema_translate_map={"lae": PARITY_SCHEMA})
        )
    try:
        yield PARITY_SCHEMA
    finally:
        with migrated_db.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{PARITY_SCHEMA}" CASCADE'))


def test_models_and_migration_produce_identical_ddl(
    migrated_db: Engine, parity_schema: str
) -> None:
    """Exact parity incl. CHECK expressions, constraint names, defaults and partial indexes."""
    with migrated_db.connect() as conn:
        assert _constraints(conn, parity_schema) == _constraints(conn, "lae")
        assert _indexes(conn, parity_schema) == _indexes(conn, "lae")
        assert _columns(conn, parity_schema) == _columns(conn, "lae")


def test_constraint_names_follow_naming_convention(migrated_db: Engine) -> None:
    prefixes = {"p": "pk_", "u": "uq_", "c": "ck_", "f": "fk_"}
    with migrated_db.connect() as conn:
        for table, name, contype, _ in _constraints(conn, "lae"):
            assert name.startswith(prefixes[contype] + table), (table, name)
        for table, name, _ in _indexes(conn, "lae"):
            assert name.startswith(("pk_", "uq_", "ix_")), (table, name)
            assert table in name


def test_triggers_installed(migrated_db: Engine) -> None:
    with migrated_db.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT tgname, tgrelid::regclass::text FROM pg_trigger "
                "WHERE NOT tgisinternal AND tgrelid::regclass::text LIKE 'lae.%'"
            )
        ).all()
    assert set(rows) == {
        ("trg_audit_event_append_only", "lae.audit_event"),
        ("trg_audit_event_append_only_truncate", "lae.audit_event"),
        ("trg_verification_run_requires_committed", "lae.verification_run"),
    }


def test_reader_role_has_select_only(migrated_db: Engine) -> None:
    with migrated_db.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = 'lae_reader' AND table_schema = 'lae'"
            )
        ).all()
    assert {t for t, _ in rows} - {"alembic_version"} == LAE_TABLES
    assert {p for _, p in rows} == {"SELECT"}


def test_downgrade_then_upgrade_round_trip(
    migrated_db: Engine, migrated_url: str, alembic_runner: Any, mark_dataset_dirty: Any
) -> None:
    mark_dataset_dirty()  # the seeded lae configuration rows are dropped with the tables
    alembic_runner(migrated_url, "base", downgrade=True)
    with migrated_db.connect() as conn:
        assert _tables(conn, "lae") == {"alembic_version"}
        fns = conn.execute(
            text(
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'lae'"
            )
        ).scalar_one()
        assert fns == 0
    alembic_runner(migrated_url, "head")
    with migrated_db.connect() as conn:
        assert _tables(conn, "lae") == LAE_TABLES | {"alembic_version"}
