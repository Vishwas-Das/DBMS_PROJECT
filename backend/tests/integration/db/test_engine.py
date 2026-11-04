"""make_engine / session_scope."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from lae.config import Settings
from lae.db.engine import is_serialization_failure, make_engine, normalize_url, session_scope
from lae.errors import ConfigurationError

pytestmark = pytest.mark.integration


@pytest.fixture
def settings(migrated_url: str) -> Settings:
    plain = migrated_url.replace("postgresql+psycopg://", "postgresql://")
    return Settings(database_url=plain, statement_timeout_ms=12_345, lock_timeout_ms=2_000)


def test_make_engine_roles_and_timeouts(settings: Settings, migrated_db: object) -> None:
    for role in ("owner", "reader", "executor"):
        engine = make_engine(settings, role)  # type: ignore[arg-type]
        try:
            with engine.connect() as conn:
                assert conn.execute(text("SHOW statement_timeout")).scalar_one() == "12345ms"
                assert conn.execute(text("SHOW lock_timeout")).scalar_one() == "2s"
                assert conn.execute(text("SHOW TimeZone")).scalar_one() == "UTC"
        finally:
            engine.dispose()
    with pytest.raises(ConfigurationError):
        make_engine(settings, "superuser")  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError):
        make_engine(Settings(database_url="sqlite:///x.db"), "owner")
    with pytest.raises(ConfigurationError):
        make_engine(Settings(database_url="::not a url::"), "owner")


def test_normalize_url() -> None:
    assert normalize_url("postgresql://u@h/d") == "postgresql+psycopg://u@h/d"
    assert normalize_url("postgres://u@h/d") == "postgresql+psycopg://u@h/d"
    assert normalize_url("postgresql+psycopg://u@h/d") == "postgresql+psycopg://u@h/d"


def test_session_scope_commit_rollback_isolation_readonly(
    settings: Settings, migrated_db: object
) -> None:
    engine = make_engine(settings, "owner")
    try:
        with session_scope(engine, isolation="serializable", read_only=True) as s:
            assert s.execute(text("SHOW transaction_isolation")).scalar_one() == "serializable"
            assert s.execute(text("SHOW transaction_read_only")).scalar_one() == "on"
            with pytest.raises(DBAPIError):
                s.execute(text("CREATE TABLE lae.should_not_exist (x int)"))
            s.rollback()
        with pytest.raises(RuntimeError), session_scope(engine) as s:
            s.execute(text("CREATE TABLE lae.scope_probe (x int)"))
            raise RuntimeError("boom")
        with session_scope(engine) as s:
            assert s.execute(text("SELECT to_regclass('lae.scope_probe')")).scalar_one() is None
        with pytest.raises(ConfigurationError), session_scope(engine, isolation="chaos"):
            pass
        with pytest.raises(DBAPIError) as info, session_scope(engine) as s:
            s.execute(text("SELECT 1/0"))
        assert not is_serialization_failure(info.value)
    finally:
        engine.dispose()


def test_serialization_failure_is_translated(settings: Settings, migrated_db: object) -> None:
    from lae.errors import SerializationConflict

    engine = make_engine(settings, "owner")
    try:
        with session_scope(engine) as s:
            s.execute(text("CREATE TABLE IF NOT EXISTS lae.ser_probe (id int PRIMARY KEY, v int)"))
            s.execute(text("TRUNCATE lae.ser_probe"))
            s.execute(text("INSERT INTO lae.ser_probe VALUES (1, 0), (2, 0)"))
        with (
            pytest.raises(SerializationConflict),
            session_scope(engine, isolation="SERIALIZABLE") as a,
        ):
            a.execute(text("SELECT sum(v) FROM lae.ser_probe")).scalar_one()
            with session_scope(engine, isolation="SERIALIZABLE") as b:
                b.execute(text("SELECT sum(v) FROM lae.ser_probe")).scalar_one()
                b.execute(text("UPDATE lae.ser_probe SET v = 1 WHERE id = 1"))
            a.execute(text("UPDATE lae.ser_probe SET v = 1 WHERE id = 2"))
    finally:
        with session_scope(engine) as s:
            s.execute(text("DROP TABLE IF EXISTS lae.ser_probe"))
        engine.dispose()
