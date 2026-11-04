"""pg_catalog queries and `PgCatalogReader` (architecture.md §6, module_contracts.md §3-§4).

Relations: pg_class.relkind r, p (TABLE), v (VIEW), m (MATERIALIZED_VIEW); partitions are
reported through their parent only. Columns via format_type(); PKs from pg_index; FKs with
ordered composite columns and confdeltype; view definitions via pg_get_viewdef(oid, true).
All catalog SQL is static with bound parameters.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg import sql
from sqlalchemy import Connection
from sqlalchemy.orm import Session

from lae.contracts.enums import OnDeleteAction, TableKind
from lae.contracts.ports import ForeignKeyRow
from lae.contracts.types import ColumnInfo, ColumnRef, KeyValue, TableInfo, TableRef
from lae.db._sql import (
    col_ident,
    driver_connection,
    out_value,
    table_ident,
)
from lae.errors import CatalogError, UnknownColumnError, UnknownTableError, ValidationError

RELKIND_TO_KIND: dict[str, TableKind] = {
    "r": TableKind.TABLE,
    "p": TableKind.TABLE,
    "v": TableKind.VIEW,
    "m": TableKind.MATERIALIZED_VIEW,
}

TABLES_SQL = """
SELECT c.oid, n.nspname, c.relname, c.relkind,
       CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END AS estimated_rows,
       CASE WHEN c.relkind IN ('v', 'm') THEN pg_get_viewdef(c.oid, true) END AS definition
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm')
  AND NOT c.relispartition
  AND n.nspname = ANY(%(schemas)s)
ORDER BY n.nspname, c.relname
"""

COLUMNS_SQL = """
SELECT a.attrelid, a.attname, a.attnum, format_type(a.atttypid, a.atttypmod), a.attnotnull
FROM pg_attribute a
WHERE a.attrelid = ANY(%(oids)s) AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attrelid, a.attnum
"""

PRIMARY_KEYS_SQL = """
SELECT i.indrelid, array_agg(a.attname ORDER BY k.ord) AS columns
FROM pg_index i
CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
WHERE i.indisprimary AND i.indrelid = ANY(%(oids)s)
GROUP BY i.indrelid
"""

# architecture.md §6 (ordered composite columns, confdeltype); ORDER BY added for determinism.
FOREIGN_KEYS_SQL = """
SELECT c.oid, c.conname,
       ns.nspname  AS child_schema,  cl.relname  AS child_table,
       nsp.nspname AS parent_schema, clp.relname AS parent_table,
       array_agg(ac.attname ORDER BY k.ord) AS child_columns,
       array_agg(ap.attname ORDER BY k.ord) AS parent_columns,
       c.confdeltype, c.confupdtype, c.condeferrable, c.condeferred
FROM pg_constraint c
JOIN pg_class cl  ON cl.oid  = c.conrelid   JOIN pg_namespace ns  ON ns.oid  = cl.relnamespace
JOIN pg_class clp ON clp.oid = c.confrelid  JOIN pg_namespace nsp ON nsp.oid = clp.relnamespace
CROSS JOIN LATERAL unnest(c.conkey, c.confkey) WITH ORDINALITY AS k(child_att, parent_att, ord)
JOIN pg_attribute ac ON ac.attrelid = c.conrelid  AND ac.attnum = k.child_att
JOIN pg_attribute ap ON ap.attrelid = c.confrelid AND ap.attnum = k.parent_att
WHERE c.contype = 'f' AND ns.nspname = ANY(%(schemas)s)
GROUP BY c.oid, c.conname, ns.nspname, cl.relname, nsp.nspname, clp.relname,
         c.confdeltype, c.confupdtype, c.condeferrable, c.condeferred
ORDER BY ns.nspname, cl.relname, c.conname
"""

COLUMN_EXISTS_SQL = """
SELECT c.relkind, a.attname IS NOT NULL
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attribute a
       ON a.attrelid = c.oid AND a.attname = %(column)s AND a.attnum > 0 AND NOT a.attisdropped
WHERE n.nspname = %(schema)s AND c.relname = %(table)s AND c.relkind IN ('r', 'p', 'v', 'm')
"""


def _key_value(value: Any) -> KeyValue:
    v = out_value(value)
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int | str):
        return v
    return str(v)


class PgCatalogReader:
    """`CatalogReader` over pg_catalog. Runs in the caller's transaction; never commits."""

    def __init__(self, conn: Connection | Session) -> None:
        self._conn = conn

    def _fetch(self, query: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
        raw = driver_connection(self._conn)
        try:
            with raw.cursor() as cur:
                cur.execute(query, params)  # static catalog SQL, bound parameters only
                return list(cur.fetchall())
        except psycopg.Error as exc:
            raise CatalogError(
                "catalog query failed", details={"sqlstate": getattr(exc, "sqlstate", None)}
            ) from None

    @staticmethod
    def _schemas(schemas: Sequence[str]) -> list[str]:
        if isinstance(schemas, str) or not schemas:
            raise CatalogError("at least one schema name is required")
        return sorted(set(schemas))

    def list_tables(self, schemas: Sequence[str]) -> list[TableInfo]:
        rels = self._fetch(TABLES_SQL, {"schemas": self._schemas(schemas)})
        if not rels:
            return []
        oids = [r[0] for r in rels]
        columns: dict[int, list[ColumnInfo]] = defaultdict(list)
        for relid, name, attnum, data_type, notnull in self._fetch(COLUMNS_SQL, {"oids": oids}):
            columns[relid].append(ColumnInfo(name, int(attnum), str(data_type), not notnull))
        pks = {relid: tuple(cols) for relid, cols in self._fetch(PRIMARY_KEYS_SQL, {"oids": oids})}
        out: list[TableInfo] = []
        for oid, schema, name, relkind, est, definition in rels:
            out.append(
                TableInfo(
                    ref=TableRef(schema, name),
                    kind=RELKIND_TO_KIND[relkind],
                    columns=tuple(columns.get(oid, ())),
                    primary_key=pks.get(oid, ()),
                    estimated_rows=None if est is None else int(est),
                    definition_sql=definition,
                )
            )
        return out

    def list_foreign_keys(self, schemas: Sequence[str]) -> list[ForeignKeyRow]:
        rows = self._fetch(FOREIGN_KEYS_SQL, {"schemas": self._schemas(schemas)})
        return [
            ForeignKeyRow(
                constraint_name=conname,
                child=TableRef(cs, ct),
                parent=TableRef(ps, pt),
                child_columns=tuple(ccols),
                parent_columns=tuple(pcols),
                on_delete=OnDeleteAction(deltype),
                on_update=OnDeleteAction(updtype),
                deferrable=bool(deferrable),
            )
            for (
                _oid,
                conname,
                cs,
                ct,
                ps,
                pt,
                ccols,
                pcols,
                deltype,
                updtype,
                deferrable,
                _deferred,
            ) in rows
        ]

    def _validate(self, col: ColumnRef) -> None:
        rows = self._fetch(
            COLUMN_EXISTS_SQL,
            {"schema": col.table.schema, "table": col.table.name, "column": col.column},
        )
        if not rows:
            raise UnknownTableError(
                f"unknown table {col.table.qualified()}", details={"table": col.table.qualified()}
            )
        if not rows[0][1]:
            raise UnknownColumnError(
                f"unknown column {col.qualified()}",
                details={"table": col.table.qualified(), "column": col.column},
            )

    def sample_values(self, col: ColumnRef, limit: int) -> list[KeyValue]:
        """Up to `limit` distinct non-NULL values, deterministic spread (ordered by md5 of text)."""
        if limit < 1:
            raise ValidationError("sample limit must be >= 1")
        self._validate(col)
        query = sql.SQL(
            "SELECT v FROM (SELECT DISTINCT {c} AS v FROM {t} WHERE {c} IS NOT NULL) s "
            "ORDER BY md5(v::text), v::text LIMIT %(limit)s"
        ).format(c=col_ident(col.column), t=table_ident(col.table))
        raw = driver_connection(self._conn)
        try:
            with raw.cursor() as cur:
                cur.execute(query, {"limit": int(limit)})
                return [_key_value(r[0]) for r in cur.fetchall()]
        except psycopg.Error as exc:
            raise CatalogError(
                "sample query failed", details={"sqlstate": getattr(exc, "sqlstate", None)}
            ) from None

    def count_contained(
        self, child: ColumnRef, parent: ColumnRef, sample: Sequence[KeyValue]
    ) -> int:
        """How many DISTINCT sample values (taken from `child`) exist in `parent` (text compare)."""
        self._validate(child)
        self._validate(parent)
        distinct = sorted({str(v) for v in sample})
        if not distinct:
            return 0
        query = sql.SQL(
            "SELECT count(DISTINCT {c}::text) FROM {t} WHERE {c}::text = ANY(%(sample)s)"
        ).format(c=col_ident(parent.column), t=table_ident(parent.table))
        raw = driver_connection(self._conn)
        try:
            with raw.cursor() as cur:
                cur.execute(query, {"sample": distinct})
                row = cur.fetchone()
                return int(row[0]) if row else 0
        except psycopg.Error as exc:
            raise CatalogError(
                "containment query failed", details={"sqlstate": getattr(exc, "sqlstate", None)}
            ) from None
