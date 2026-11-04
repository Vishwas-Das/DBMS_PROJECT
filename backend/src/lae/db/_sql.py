"""Low-level helpers shared by the target-schema adapters.

* Identifiers are only ever emitted through `psycopg.sql.Identifier` after the caller validated
  them against a catalog. Values are only ever bound parameters.
* Database errors are translated to `lae.errors` types WITHOUT the server's DETAIL text (which
  can echo row values such as an email in a unique-violation message).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from psycopg import sql
from sqlalchemy import Connection
from sqlalchemy.orm import Session

from lae.contracts.types import KeyTuple, TableRef
from lae.errors import LaeError, SerializationConflict, TargetQueryError, ValidationError

DEFAULT_BATCH_SIZE = 1_000
_SAFE_NAME = re.compile(r"^[^\x00]{1,63}$")
SERIALIZATION_SQLSTATES = frozenset({"40001", "40P01"})

TEXT_TYPES = frozenset({"text", "citext", "name"})
TEXT_PREFIXES = ("character varying", "character", "varchar", "char")
JSON_TYPES = frozenset({"json", "jsonb"})
INT_TYPES = frozenset({"smallint", "integer", "bigint"})


def is_text_like(data_type: str) -> bool:
    """`text`, `varchar(n)`, `char(n)`, `citext`, `json`, `jsonb` (the residual-scan surface)."""
    dt = data_type.lower()
    return dt in TEXT_TYPES or dt in JSON_TYPES or dt.startswith(TEXT_PREFIXES)


def is_json(data_type: str) -> bool:
    return data_type.lower() in JSON_TYPES


def check_name(name: str) -> str:
    """Last line of defence: PostgreSQL identifiers are 1..63 bytes without NUL."""
    if not isinstance(name, str) or not _SAFE_NAME.match(name) or len(name.encode()) > 63:
        raise ValidationError("invalid SQL identifier", details={"identifier_length": len(name)})
    return name


def table_ident(ref: TableRef) -> sql.Identifier:
    return sql.Identifier(check_name(ref.schema), check_name(ref.name))


def col_ident(name: str) -> sql.Identifier:
    return sql.Identifier(check_name(name))


def col_list(names: Sequence[str]) -> sql.Composed:
    return sql.SQL(", ").join(col_ident(n) for n in names)


def driver_connection(conn: Connection | Session) -> psycopg.Connection[Any]:
    """The psycopg connection behind a SQLAlchemy Connection/Session, inside its transaction."""
    if isinstance(conn, Session):
        conn = conn.connection()
    elif not conn.in_transaction():
        conn.begin()
    raw = conn.connection.driver_connection
    if not isinstance(raw, psycopg.Connection):  # pragma: no cover - driver is pinned
        raise TargetQueryError("lae.db requires the psycopg 3 driver")
    return raw


# format_type() output we accept as a cast target, e.g. bigint, character varying(20),
# timestamp with time zone, numeric(4,3), vector(384), text[]. Never free text.
_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*( [a-z][a-z0-9_]*)*( ?\([0-9]+(,[0-9]+)?\))?(\[\])*$")


def cast_type(data_type: str) -> sql.SQL:
    """A validated catalog type (format_type output) usable as `%s::<type>`."""
    if not _TYPE_PATTERN.match(data_type) or len(data_type) > 80:
        raise ValidationError(
            "unsupported column type for key binding", details={"data_type_length": len(data_type)}
        )
    return sql.SQL(data_type)


def adapt_value(value: Any, data_type: str = "") -> Any:
    """Key values are bound as text and cast server-side to the column's catalog type, so
    '42' matches a bigint column and a uuid string matches a uuid column."""
    if isinstance(value, bool):
        raise ValidationError("boolean key values are not supported")
    if isinstance(value, int | str):
        return str(value)
    raise ValidationError("key values must be int or str")


def out_value(value: Any) -> Any:
    """Normalize driver output like discovery does: uuid -> str, integral Decimal -> int."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        return int(value)
    return value


def dedupe(values: Sequence[KeyTuple]) -> list[KeyTuple]:
    seen: set[KeyTuple] = set()
    out: list[KeyTuple] = []
    for v in values:
        t = tuple(v)
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def batches(values: Sequence[KeyTuple], size: int) -> list[list[KeyTuple]]:
    return [list(values[i : i + size]) for i in range(0, len(values), size)]


def values_list(n_rows: int, types: Sequence[str]) -> sql.Composed:
    """`(%s::t1, ..., %s::tn), (...)`: typed placeholders only."""
    row = sql.SQL("({})").format(
        sql.SQL(", ").join(sql.SQL("{}::{}").format(sql.Placeholder(), cast_type(t)) for t in types)
    )
    return sql.SQL(", ").join([row] * n_rows)


def in_values(columns: Sequence[str], types: Sequence[str], n_rows: int) -> sql.Composed:
    """`(c1, ..., cn) IN (VALUES (%s::t1, ...), ...)`."""
    return sql.SQL("({}) IN (VALUES {})").format(col_list(columns), values_list(n_rows, types))


def sort_token(value: Any) -> tuple[int, Any]:
    """Total, platform-independent order for heterogeneous row values (None first)."""
    if value is None:
        return (0, 0)
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, int):
        return (2, value)
    if isinstance(value, str):
        return (3, value)
    return (4, str(value))


def translate_error(exc: psycopg.Error, table: TableRef | None = None) -> LaeError:
    """Map a driver error to lae.errors without leaking values (no DETAIL / params)."""
    sqlstate = getattr(exc, "sqlstate", None)
    diag = getattr(exc, "diag", None)
    details: dict[str, Any] = {"sqlstate": sqlstate}
    if table is not None:
        details["table"] = table.qualified()
    constraint = getattr(diag, "constraint_name", None) if diag is not None else None
    if constraint:
        details["constraint"] = constraint
    if sqlstate in SERIALIZATION_SQLSTATES:
        return SerializationConflict("serialization failure", details=details)
    where = f" on {table.qualified()}" if table is not None else ""
    return TargetQueryError(f"target query failed (SQLSTATE {sqlstate}){where}", details=details)


def assert_single_select(statement: str) -> str:
    """Security review M1: an artifact definition must be exactly ONE query (SELECT/UNION/...),
    with no DML, DDL, transaction control or data-modifying CTE. Parsed with SQLGlot; anything
    unparseable is rejected. Returns the statement unchanged."""
    import sqlglot
    from sqlglot import exp

    # bound-parameter placeholders are not SQL; replace them with NULL for parsing only
    probe = re.sub(r"%\(\w+\)s|%s", "NULL", statement)
    try:
        trees = [t for t in sqlglot.parse(probe, read="postgres") if t is not None]
    except sqlglot.errors.SqlglotError:
        raise ValidationError("artifact definition is not parseable SQL") from None
    if len(trees) != 1 or not isinstance(trees[0], exp.Query):
        raise ValidationError("artifact definition must be exactly one SELECT query")
    forbidden = (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Merge,
        exp.Create,
        exp.Drop,
        exp.Alter,
        exp.Command,
        exp.Transaction,
        exp.Commit,
        exp.Rollback,
        exp.Into,
    )
    if any(isinstance(node, forbidden) for node in trees[0].walk()):
        raise ValidationError("artifact definition may not modify data or schema")
    return statement
