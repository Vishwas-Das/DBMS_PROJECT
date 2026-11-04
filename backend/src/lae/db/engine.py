"""Engines and transaction scopes (module_contracts.md §4).

The service layer owns transactions: `session_scope` commits on success and rolls back on any
exception. SQLSTATE 40001 / 40P01 (serialization failure / deadlock) is re-raised as
`lae.errors.SerializationConflict` (no statement parameters are carried along).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, DBAPIError
from sqlalchemy.orm import Session

from lae.config import Settings
from lae.errors import ConfigurationError, SerializationConflict

Role = Literal["owner", "reader", "executor"]
IsolationLevel = Literal["READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"]
ISOLATION_LEVELS: frozenset[str] = frozenset({"READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"})
SERIALIZATION_SQLSTATES = frozenset({"40001", "40P01"})


def normalize_url(url: str) -> str:
    """Force the psycopg 3 driver (`postgresql://` -> `postgresql+psycopg://`)."""
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    return url


def make_engine(settings: Settings, role: Role) -> Engine:
    """SQLAlchemy engine for a DB role. Applies statement/lock timeouts per connection."""
    raw = settings.url_for(role)
    try:
        url = make_url(normalize_url(raw))
    except ArgumentError:
        raise ConfigurationError(f"invalid database URL for role {role!r}") from None
    if url.get_backend_name() != "postgresql":
        raise ConfigurationError(f"role {role!r}: only PostgreSQL is supported")
    # hide_parameters: statement parameters (subject keys, identifier values) never appear in
    # SQLAlchemy exception messages or logs.
    engine = create_engine(url, pool_pre_ping=True, future=True, hide_parameters=True)
    statement_ms = int(settings.statement_timeout_ms)
    lock_ms = int(settings.lock_timeout_ms)

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn: object, _record: object) -> None:  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
        try:
            # set_config() with bound values: no SQL is built from settings.
            cur.execute("SELECT set_config('statement_timeout', %s, false)", (f"{statement_ms}",))
            cur.execute("SELECT set_config('lock_timeout', %s, false)", (f"{lock_ms}",))
            cur.execute("SELECT set_config('TimeZone', 'UTC', false)")
        finally:
            cur.close()
        dbapi_conn.commit()  # type: ignore[attr-defined]

    return engine


def is_serialization_failure(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", exc)
    return getattr(orig, "sqlstate", None) in SERIALIZATION_SQLSTATES


@contextmanager
def session_scope(
    engine: Engine, *, isolation: str | None = None, read_only: bool = False
) -> Iterator[Session]:
    """One transaction: commit on success, roll back on exception.

    `isolation` is one of READ COMMITTED / REPEATABLE READ / SERIALIZABLE. `read_only` issues
    `SET TRANSACTION READ ONLY` as the first statement of the transaction.
    """
    bind: Engine = engine
    if isolation is not None:
        level = isolation.upper()
        if level not in ISOLATION_LEVELS:
            raise ConfigurationError(f"unsupported isolation level {isolation!r}")
        bind = engine.execution_options(isolation_level=level)
    session = Session(bind=bind, expire_on_commit=False, autoflush=True)
    try:
        if read_only:
            session.execute(text("SET TRANSACTION READ ONLY"))
        yield session
        session.commit()
    except DBAPIError as exc:
        session.rollback()
        if is_serialization_failure(exc):
            raise SerializationConflict(
                "serialization failure; re-plan and retry",
                details={"sqlstate": getattr(exc.orig, "sqlstate", None)},
            ) from None
        raise
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
