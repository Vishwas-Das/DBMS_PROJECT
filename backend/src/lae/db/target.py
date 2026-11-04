"""`PgTargetReader` / `PgTargetWriter`: the TargetReader / TargetWriter ports over PostgreSQL.

Rules (module_contracts.md §3-§4, CLAUDE.md security):
* every table / column is validated against the bound `CatalogSnapshot` first
  (`UnknownTableError` / `UnknownColumnError`) and only then emitted via `psycopg.sql.Identifier`;
* values are ALWAYS bound parameters; key sets go through
  `WHERE (c1, ..., cn) IN (VALUES (%s, ...), ...)` in batches of at most `batch_size` tuples;
* results are deterministically ordered (by primary key, else by the projected columns);
* the only statements built from non-identifier text are registered artifact definitions
  (`run_readonly_sql`, `symmetric_difference_count`, `recompute_table`); the reads run inside a
  savepoint switched to `READ ONLY`; an optional allow-list restricts them to registered SQL;
* nothing here commits; everything runs in the caller's transaction;
* errors never carry row values (see `lae.db._sql.translate_error`).
"""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg import sql
from sqlalchemy import Connection
from sqlalchemy.orm import Session

from lae.contracts.enums import Normalization, TableKind
from lae.contracts.types import (
    CatalogSnapshot,
    ColumnAssignment,
    ColumnRef,
    KeyTuple,
    TableInfo,
    TableRef,
)
from lae.db._sql import (
    DEFAULT_BATCH_SIZE,
    adapt_value,
    batches,
    cast_type,
    col_ident,
    col_list,
    dedupe,
    driver_connection,
    in_values,
    is_json,
    is_text_like,
    out_value,
    sort_token,
    table_ident,
    translate_error,
)
from lae.errors import ConfigurationError, TargetQueryError, ValidationError

# CR verification-crypto-engineer-1 §5 (Lead decision + correction): ASCII-only, locale-free.
# PostgreSQL E'' strings have no \v escape, so VT is written as \x0B.
LOWER_TRIM_SQL = (
    "translate(btrim({x}, E' \\t\\n\\r\\f\\x0B'), "
    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
)
DIGITS_ONLY_SQL = "regexp_replace({x}, '[^0-9]', '', 'g')"
PSEUDONYM_PREFIX = "anon_"
_RO_SAVEPOINT = sql.Identifier("lae_readonly")


def _strip_sql(definition_sql: str) -> str:
    s = definition_sql.strip()
    while s.endswith(";"):
        s = s[:-1].rstrip()
    if not s:
        raise ValidationError("empty artifact definition")
    return s


class PgTargetReader:
    """Read-only access to the target schemas of `catalog` through `conn` (never commits).

    Methods: select_by_columns, count_by_columns, fetch_columns, run_readonly_sql,
    hmac_match_count, symmetric_difference_count.
    """

    def __init__(
        self,
        conn: Connection | Session,
        catalog: CatalogSnapshot,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        allowed_sql: Collection[str] | None = None,
    ) -> None:
        if not 1 <= batch_size <= DEFAULT_BATCH_SIZE:
            raise ConfigurationError(f"batch_size must be within 1..{DEFAULT_BATCH_SIZE}")
        self._conn = conn
        self._catalog = catalog
        self._batch_size = batch_size
        self._allowed = None if allowed_sql is None else {_strip_sql(s) for s in allowed_sql}

    # ------------------------------------------------------------ validation
    @property
    def catalog(self) -> CatalogSnapshot:
        return self._catalog

    def _table(self, ref: TableRef) -> TableInfo:
        info = self._catalog.table(ref)  # UnknownTableError
        if ref.schema not in self._catalog.schemas:
            raise ValidationError(
                "table is outside the target schemas", details={"table": ref.qualified()}
            )
        return info

    def _columns(self, info: TableInfo, columns: Sequence[str]) -> list[str]:
        if isinstance(columns, str):
            raise ValidationError("columns must be a sequence of names, not a string")
        for c in columns:
            info.column(c)  # UnknownColumnError
        return list(columns)

    def _keys(
        self, info: TableInfo, columns: Sequence[str], values: Sequence[KeyTuple]
    ) -> list[KeyTuple]:
        if not columns:
            raise ValidationError("at least one key column is required")
        types = [info.column(c).data_type for c in columns]
        out: list[KeyTuple] = []
        for v in dedupe(values):
            if len(v) != len(columns):
                raise ValidationError(
                    "key tuple arity does not match key columns",
                    details={"table": info.ref.qualified(), "columns": len(columns)},
                )
            out.append(tuple(adapt_value(x, t) for x, t in zip(v, types, strict=True)))
        return out

    @staticmethod
    def _types(info: TableInfo, columns: Sequence[str]) -> list[str]:
        return [info.column(c).data_type for c in columns]

    def _order(self, info: TableInfo, select: Sequence[str]) -> list[str]:
        return list(info.primary_key) if info.primary_key else list(select)

    def _sql_allowed(self, statement: str) -> str:
        s = _strip_sql(statement)
        if self._allowed is not None and s not in self._allowed:
            raise ValidationError("SQL is not a registered artifact definition")
        return s

    # ------------------------------------------------------------ execution
    def _raw(self) -> psycopg.Connection[Any]:
        return driver_connection(self._conn)

    def _exec(
        self,
        query: sql.SQL | sql.Composed,
        params: Sequence[Any] | Mapping[str, Any] | None,
        table: TableRef | None,
    ) -> psycopg.Cursor[Any]:
        cur = self._raw().cursor()
        try:
            cur.execute(query, params)
        except psycopg.Error as exc:
            cur.close()
            raise translate_error(exc, table) from None
        return cur

    @contextmanager
    def _read_only(self) -> Iterator[None]:
        """Savepoint + SET TRANSACTION READ ONLY; always rolled back to (state restored)."""
        raw = self._raw()
        with raw.cursor() as cur:
            cur.execute(sql.SQL("SAVEPOINT {}").format(_RO_SAVEPOINT))
            try:
                cur.execute("SET TRANSACTION READ ONLY")
                yield
            finally:
                cur.execute(sql.SQL("ROLLBACK TO SAVEPOINT {}").format(_RO_SAVEPOINT))
                cur.execute(sql.SQL("RELEASE SAVEPOINT {}").format(_RO_SAVEPOINT))

    # ------------------------------------------------------------ port methods
    def select_by_columns(
        self,
        table: TableRef,
        columns: Sequence[str],
        values: Sequence[KeyTuple],
        select: Sequence[str],
    ) -> list[dict[str, Any]]:
        info = self._table(table)
        cols = self._columns(info, columns)
        proj = self._columns(info, select)
        if not proj:
            raise ValidationError("select must name at least one column")
        keys = self._keys(info, cols, values)
        if not keys:
            return []
        order = self._order(info, proj)
        # Fetch the ORDER BY columns too, so the final ordering is total even across batches.
        fetch = list(dict.fromkeys([*proj, *order]))
        rows: list[dict[str, Any]] = []
        for batch in batches(keys, self._batch_size):
            query = sql.SQL("SELECT {sel} FROM {t} WHERE {pred} ORDER BY {order}").format(
                sel=col_list(fetch),
                t=table_ident(table),
                pred=in_values(cols, self._types(info, cols), len(batch)),
                order=col_list(order),
            )
            with self._exec(query, [x for k in batch for x in k], table) as cur:
                for r in cur.fetchall():
                    rows.append({c: out_value(v) for c, v in zip(fetch, r, strict=True)})
        rows.sort(key=lambda r: tuple(sort_token(r[c]) for c in order))
        return [{c: r[c] for c in proj} for r in rows]

    def count_by_columns(
        self, table: TableRef, columns: Sequence[str], values: Sequence[KeyTuple]
    ) -> int:
        info = self._table(table)
        cols = self._columns(info, columns)
        keys = self._keys(info, cols, values)
        total = 0
        for batch in batches(keys, self._batch_size):
            query = sql.SQL("SELECT count(*) FROM {t} WHERE {pred}").format(
                t=table_ident(table), pred=in_values(cols, self._types(info, cols), len(batch))
            )
            with self._exec(query, [x for k in batch for x in k], table) as cur:
                row = cur.fetchone()
                total += int(row[0]) if row else 0
        return total

    def fetch_columns(
        self, table: TableRef, key_columns: Sequence[str], key: KeyTuple, columns: Sequence[str]
    ) -> dict[str, Any] | None:
        rows = self.select_by_columns(table, key_columns, [key], columns)
        if len(rows) > 1:
            raise TargetQueryError(
                "key matched more than one row",
                details={"table": table.qualified(), "rows": len(rows)},
            )
        return rows[0] if rows else None

    def run_readonly_sql(self, sql_text: str, params: Mapping[str, Any]) -> list[tuple[Any, ...]]:
        """Execute a REGISTERED artifact definition read-only (savepoint, rolled back)."""
        statement = self._sql_allowed(sql_text)
        # The statement is a registered artifact definition, never user/request input.
        with self._read_only(), self._exec(sql.SQL(statement), dict(params) or None, None) as cur:
            return [tuple(out_value(v) for v in r) for r in cur.fetchall()]

    def hmac_match_count(
        self,
        col: ColumnRef,
        digests: Sequence[str],
        normalization: Normalization,
        hmac_key: bytes,
    ) -> int:
        """Rows of `col` whose normalized-value HMAC-SHA256 (pgcrypto) is in `digests`."""
        info = self._table(col.table)
        column = info.column(col.column)
        if not is_text_like(column.data_type):
            raise ValidationError(
                "hmac_match_count needs a text-like column",
                details={"column": col.qualified(), "data_type": column.data_type},
            )
        wanted = sorted({d.lower() for d in digests})
        if not wanted:
            return 0
        if not isinstance(hmac_key, bytes | bytearray) or len(hmac_key) < 32:
            raise ConfigurationError("HMAC key must be at least 32 bytes")
        ident: sql.Composable = col_ident(col.column)
        x: sql.Composable = (
            sql.SQL("({}::text)").format(ident) if is_json(column.data_type) else ident
        )
        if normalization is Normalization.LOWER_TRIM:
            norm: sql.Composable = sql.SQL(LOWER_TRIM_SQL).format(x=x)
        elif normalization is Normalization.DIGITS_ONLY:
            norm = sql.SQL(DIGITS_ONLY_SQL).format(x=x)
        elif normalization is Normalization.EXACT:
            norm = sql.SQL("({}::text)").format(x)
        else:  # pragma: no cover - enum is closed
            raise ValidationError(f"unsupported normalization {normalization!r}")
        query = sql.SQL(
            "SELECT count(*) FROM {t} WHERE encode(hmac(convert_to({n}, 'UTF8'), %s, 'sha256'), "
            "'hex') = ANY(%s)"
        ).format(t=table_ident(col.table), n=norm)
        with self._exec(query, (bytes(hmac_key), wanted), col.table) as cur:
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def symmetric_difference_count(self, stored: TableRef, definition_sql: str) -> int:
        """|stored EXCEPT ALL fresh| + |fresh EXCEPT ALL stored|, fresh = the definition now."""
        self._table(stored)
        fresh = sql.SQL(self._sql_allowed(definition_sql))
        query = sql.SQL(
            "SELECT count(*) FROM ((SELECT * FROM {t} EXCEPT ALL ({f})) "
            "UNION ALL (({f}) EXCEPT ALL SELECT * FROM {t})) AS d"
        ).format(t=table_ident(stored), f=fresh)
        with self._read_only(), self._exec(query, None, stored) as cur:
            row = cur.fetchone()
            return int(row[0]) if row else 0


class PgTargetWriter(PgTargetReader):
    """TargetWriter: DML on target tables in the caller's (executor) transaction. Never commits.

    Methods (in addition to the reader's): lock_rows, delete_rows, update_columns,
    refresh_matview, recompute_table. `hmac_key` is needed only for PSEUDONYM assignments.
    """

    def __init__(
        self,
        conn: Connection | Session,
        catalog: CatalogSnapshot,
        *,
        hmac_key: bytes | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        allowed_sql: Collection[str] | None = None,
    ) -> None:
        super().__init__(conn, catalog, batch_size=batch_size, allowed_sql=allowed_sql)
        if hmac_key is not None and len(hmac_key) < 32:
            raise ConfigurationError("HMAC key must be at least 32 bytes")
        self.__hmac_key = None if hmac_key is None else bytes(hmac_key)

    def __repr__(self) -> str:
        return "PgTargetWriter(<redacted>)"

    def _base_table(self, ref: TableRef) -> TableInfo:
        info = self._table(ref)
        if info.kind is not TableKind.TABLE:
            raise ValidationError(
                "DML is only allowed on base tables", details={"table": ref.qualified()}
            )
        return info

    def _per_batch(
        self,
        table: TableRef,
        key_columns: Sequence[str],
        keys: Sequence[KeyTuple],
        build: Any,
        extra: Sequence[Any] = (),
    ) -> int:
        info = self._base_table(table)
        cols = self._columns(info, key_columns)
        tuples = self._keys(info, cols, keys)
        total = 0
        for batch in batches(tuples, self._batch_size):
            query = build(info, cols, len(batch))
            params = [*extra, *(x for k in batch for x in k)]
            with self._exec(query, params, table) as cur:
                total += cur.rowcount if cur.rowcount >= 0 else 0
        return total

    def lock_rows(
        self, table: TableRef, key_columns: Sequence[str], keys: Sequence[KeyTuple]
    ) -> int:
        def build(info: TableInfo, cols: list[str], n: int) -> sql.Composable:
            return sql.SQL("SELECT 1 FROM {t} WHERE {pred} FOR UPDATE").format(
                t=table_ident(info.ref), pred=in_values(cols, self._types(info, cols), n)
            )

        return self._per_batch(table, key_columns, keys, build)

    def delete_rows(
        self, table: TableRef, key_columns: Sequence[str], keys: Sequence[KeyTuple]
    ) -> int:
        def build(info: TableInfo, cols: list[str], n: int) -> sql.Composable:
            return sql.SQL("DELETE FROM {t} WHERE {pred}").format(
                t=table_ident(info.ref), pred=in_values(cols, self._types(info, cols), n)
            )

        return self._per_batch(table, key_columns, keys, build)

    def update_columns(
        self,
        table: TableRef,
        key_columns: Sequence[str],
        keys: Sequence[KeyTuple],
        assignments: Mapping[str, ColumnAssignment],
    ) -> int:
        """SET col = NULL | 'anon_' || left(encode(hmac(<row key text>, key, 'sha256'),'hex'),16).

        The row key text is the table's primary key (else `key_columns`) cast to text and
        joined with ':' in column order.
        """
        info = self._base_table(table)
        if not assignments:
            raise ValidationError("update_columns needs at least one assignment")
        targets = self._columns(info, sorted(assignments))
        key_cols = list(info.primary_key) or self._columns(info, key_columns)
        needs_key = False
        sets: list[sql.Composable] = []
        for name in targets:
            kind = assignments[name].kind
            if kind == "NULL":
                sets.append(sql.SQL("{} = NULL").format(col_ident(name)))
            elif kind == "PSEUDONYM":
                if not is_text_like(info.column(name).data_type) or is_json(
                    info.column(name).data_type
                ):
                    raise ValidationError(
                        "PSEUDONYM needs a text column",
                        details={"table": table.qualified(), "column": name},
                    )
                needs_key = True
                key_text = sql.SQL(" || ':' || ").join(
                    sql.SQL("{}::text").format(col_ident(k)) for k in key_cols
                )
                sets.append(
                    sql.SQL(
                        "{c} = %(prefix)s || left(encode(hmac(convert_to({k}, 'UTF8'), "
                        "%(key)s, 'sha256'), 'hex'), 16)"
                    ).format(c=col_ident(name), k=key_text)
                )
            else:
                raise ValidationError(f"unsupported assignment kind {kind!r}")
        if needs_key and self.__hmac_key is None:
            raise ConfigurationError("PSEUDONYM assignments need the writer's HMAC key")
        cols = self._columns(info, key_columns)
        tuples = self._keys(info, cols, keys)
        total = 0
        for batch in batches(tuples, self._batch_size):
            names = [f"k{i}" for i in range(len(batch) * len(cols))]
            row = len(cols)
            casts = [cast_type(t) for t in self._types(info, cols)]
            values = sql.SQL(", ").join(
                sql.SQL("({})").format(
                    sql.SQL(", ").join(
                        sql.SQL("{}::{}").format(sql.Placeholder(n), c)
                        for n, c in zip(names[i : i + row], casts, strict=True)
                    )
                )
                for i in range(0, len(names), row)
            )
            query = sql.SQL("UPDATE {t} SET {sets} WHERE ({kc}) IN (VALUES {v})").format(
                t=table_ident(table), sets=sql.SQL(", ").join(sets), kc=col_list(cols), v=values
            )
            params: dict[str, Any] = dict(zip(names, (x for k in batch for x in k), strict=True))
            if needs_key:
                params["prefix"] = PSEUDONYM_PREFIX
                params["key"] = self.__hmac_key
            with self._exec(query, params, table) as cur:
                total += cur.rowcount if cur.rowcount >= 0 else 0
        return total

    def refresh_matview(self, table: TableRef) -> None:
        info = self._table(table)
        if info.kind is not TableKind.MATERIALIZED_VIEW:
            raise ValidationError("not a materialized view", details={"table": table.qualified()})
        query = sql.SQL("REFRESH MATERIALIZED VIEW {}").format(table_ident(table))
        self._exec(query, None, table).close()

    def recompute_table(self, table: TableRef, definition_sql: str) -> int:
        """DELETE FROM t; INSERT INTO t <definition>. Returns the number of rows inserted."""
        self._base_table(table)
        statement = sql.SQL(self._sql_allowed(definition_sql))
        self._exec(sql.SQL("DELETE FROM {}").format(table_ident(table)), None, table).close()
        query = sql.SQL("INSERT INTO {t} {f}").format(t=table_ident(table), f=statement)
        with self._exec(query, None, table) as cur:
            return cur.rowcount if cur.rowcount >= 0 else 0
