"""Catalog snapshot + schema fingerprint (architecture.md §6, module_contracts.md §5).

The fingerprint is ``canonical_hash`` of a sorted, canonical description of every table (kind,
columns with type and nullability, primary key, view definition) plus every declared foreign key
(ordered column pairs, ON DELETE / ON UPDATE codes, deferrability). Row-count estimates are
excluded on purpose: ``ANALYZE`` must not look like schema drift.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from lae.audit.canonical import canonical_hash
from lae.contracts.ports import CatalogReader, ForeignKeyRow
from lae.contracts.types import CatalogSnapshot, TableInfo, TableRef
from lae.errors import CatalogError

__all__ = [
    "SchemaDrift",
    "build_catalog",
    "catalog_fingerprint",
    "describe_foreign_key",
    "describe_table",
    "read_catalog",
    "schema_drift",
]

_log = logging.getLogger(__name__)


def describe_table(info: TableInfo) -> dict[str, Any]:
    """Canonical, hashable description of one table (no row estimates)."""
    definition = info.definition_sql.strip() if info.definition_sql is not None else None
    return {
        "schema": info.ref.schema,
        "name": info.ref.name,
        "kind": info.kind.value,
        "columns": [
            {
                "name": c.name,
                "ordinal": c.ordinal,
                "data_type": c.data_type,
                "is_nullable": c.is_nullable,
            }
            for c in sorted(info.columns, key=lambda c: (c.ordinal, c.name))
        ],
        "primary_key": list(info.primary_key),
        "definition_sql": definition,
    }


def describe_foreign_key(fk: ForeignKeyRow) -> dict[str, Any]:
    """Canonical description of one FK. Column order is preserved (composite keys)."""
    return {
        "constraint_name": fk.constraint_name,
        "child": fk.child.qualified(),
        "parent": fk.parent.qualified(),
        "child_columns": list(fk.child_columns),
        "parent_columns": list(fk.parent_columns),
        "on_delete": fk.on_delete.value,
        "on_update": fk.on_update.value,
        "deferrable": fk.deferrable,
    }


def catalog_fingerprint(
    tables: Iterable[TableInfo],
    foreign_keys: Iterable[ForeignKeyRow],
    schemas: Sequence[str],
) -> str:
    """SHA-256 hex of the canonical catalog description. Input order does not matter."""
    doc = {
        "schemas": sorted(set(schemas)),
        "tables": [describe_table(t) for t in sorted(tables, key=lambda t: t.ref)],
        "foreign_keys": sorted(
            (describe_foreign_key(f) for f in foreign_keys),
            key=lambda d: (d["child"], d["constraint_name"]),
        ),
    }
    return canonical_hash(doc)


def _validate_tables(tables: Sequence[TableInfo], schemas: Sequence[str]) -> None:
    wanted = set(schemas)
    seen: set[TableRef] = set()
    for info in tables:
        ref = info.ref
        if ref.schema not in wanted:
            raise CatalogError(
                "catalog reader returned a table outside the requested schemas",
                details={"table": ref.qualified()},
            )
        if ref in seen:
            raise CatalogError("duplicate table in catalog", details={"table": ref.qualified()})
        seen.add(ref)
        names = [c.name for c in info.columns]
        if len(set(names)) != len(names):
            raise CatalogError("duplicate column name", details={"table": ref.qualified()})
        for pk_col in info.primary_key:
            if pk_col not in names:
                raise CatalogError(
                    "primary-key column missing from table",
                    details={"table": ref.qualified(), "column": pk_col},
                )


def build_catalog(
    tables: Iterable[TableInfo],
    foreign_keys: Iterable[ForeignKeyRow],
    schemas: Sequence[str],
) -> CatalogSnapshot:
    """Validate `tables` and build a `CatalogSnapshot` with its fingerprint."""
    if not schemas:
        raise CatalogError("at least one schema must be scanned")
    table_list = list(tables)
    fk_list = list(foreign_keys)
    _validate_tables(table_list, schemas)
    ordered = {t.ref: t for t in sorted(table_list, key=lambda t: t.ref)}
    return CatalogSnapshot(
        tables=ordered,
        schemas=tuple(sorted(set(schemas))),
        fingerprint=catalog_fingerprint(table_list, fk_list, schemas),
    )


def read_catalog(reader: CatalogReader, schemas: Sequence[str]) -> CatalogSnapshot:
    """Read tables and FKs of `schemas` through the port and build the snapshot.

    Raises CatalogError if the reader output is inconsistent.
    """
    schema_list = list(schemas)
    tables = reader.list_tables(schema_list)
    fks = reader.list_foreign_keys(schema_list)
    snapshot = build_catalog(tables, fks, schema_list)
    _log.info(
        "catalog read: %d tables, %d foreign keys, fingerprint %s",
        len(snapshot.tables),
        len(fks),
        snapshot.fingerprint[:12],
    )
    return snapshot


# ---------------------------------------------------------------------- drift


@dataclass(frozen=True, slots=True)
class SchemaDrift:
    """Difference between two snapshots. Tables are sorted.

    `changed_tables` compares table descriptions (columns, types, PK, definition). FK changes are
    not visible in a `CatalogSnapshot`, so `fingerprint_changed` is the authoritative signal.
    """

    added_tables: tuple[TableRef, ...]
    removed_tables: tuple[TableRef, ...]
    changed_tables: tuple[TableRef, ...]
    fingerprint_changed: bool

    @property
    def has_drift(self) -> bool:
        return self.fingerprint_changed or bool(
            self.added_tables or self.removed_tables or self.changed_tables
        )


def schema_drift(old: CatalogSnapshot, new: CatalogSnapshot) -> SchemaDrift:
    old_refs = set(old.tables)
    new_refs = set(new.tables)
    changed = [
        ref
        for ref in sorted(old_refs & new_refs)
        if describe_table(old.tables[ref]) != describe_table(new.tables[ref])
    ]
    return SchemaDrift(
        added_tables=tuple(sorted(new_refs - old_refs)),
        removed_tables=tuple(sorted(old_refs - new_refs)),
        changed_tables=tuple(changed),
        fingerprint_changed=old.fingerprint != new.fingerprint,
    )
