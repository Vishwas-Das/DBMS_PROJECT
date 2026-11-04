"""Declared foreign-key discovery (architecture.md §6).

Each FK gives one TABLE-level edge ``parent -> child`` carrying the ordered composite column
arrays and ``confdeltype``, plus one single-pair COLUMN-level edge per column pair (accepted
contract rule, CR graph-engineer-1 §4). Composite keys are never split into independent joins:
the TABLE edge is the unit of traversal, the COLUMN edges are descriptive.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from uuid import UUID

from lae.contracts.enums import (
    EdgeOrigin,
    EdgeStatus,
    LineageLevel,
    Traceability,
    Transform,
)
from lae.contracts.ports import CatalogReader, ForeignKeyRow
from lae.contracts.types import CatalogSnapshot, RelationshipEdge
from lae.discovery._ids import NS_FK, stable_id
from lae.errors import CatalogError

__all__ = ["discover_foreign_keys", "fk_edge_id", "foreign_key_edges"]

_log = logging.getLogger(__name__)


def fk_edge_id(fk: ForeignKeyRow, level: LineageLevel, pair_index: int | None = None) -> UUID:
    """Deterministic id material: (origin, child table, constraint, level, pair index).

    The child table is part of the name because PostgreSQL constraint names are unique per
    table, not per schema.
    """
    idx = "-" if pair_index is None else str(pair_index)
    return stable_id(
        NS_FK,
        EdgeOrigin.DECLARED_FK.value,
        fk.child.qualified(),
        fk.constraint_name,
        level.value,
        idx,
    )


def _validate(fk: ForeignKeyRow, catalog: CatalogSnapshot) -> bool:
    """True if the FK can be represented; False if its parent is outside the snapshot."""
    where = {"constraint": fk.constraint_name, "table": fk.child.qualified()}
    if not fk.child_columns or len(fk.child_columns) != len(fk.parent_columns):
        raise CatalogError("foreign key has mismatched column lists", details=where)
    if not catalog.has_table(fk.child):
        raise CatalogError("foreign key child table not in catalog", details=where)
    if not catalog.has_table(fk.parent):
        _log.warning(
            "skipping FK %s on %s: parent %s is outside the scanned schemas",
            fk.constraint_name,
            fk.child.qualified(),
            fk.parent.qualified(),
        )
        return False
    child = catalog.tables[fk.child]
    parent = catalog.tables[fk.parent]
    for c in fk.child_columns:
        if not child.has_column(c):
            raise CatalogError("foreign key column missing", details={**where, "column": c})
    for c in fk.parent_columns:
        if not parent.has_column(c):
            raise CatalogError(
                "referenced column missing",
                details={**where, "parent": fk.parent.qualified(), "column": c},
            )
    return True


def foreign_key_edges(
    fks: Iterable[ForeignKeyRow], catalog: CatalogSnapshot
) -> list[RelationshipEdge]:
    """Pure conversion of FK rows into lineage edges, sorted by (child, constraint, level, pair).

    Raises CatalogError for inconsistent rows or a duplicate (child, constraint_name).
    """
    rows = sorted(fks, key=lambda f: (f.child, f.constraint_name))
    seen: set[tuple[object, str]] = set()
    edges: list[RelationshipEdge] = []
    for fk in rows:
        ident = (fk.child, fk.constraint_name)
        if ident in seen:
            raise CatalogError(
                "duplicate foreign-key constraint",
                details={"constraint": fk.constraint_name, "table": fk.child.qualified()},
            )
        seen.add(ident)
        if not _validate(fk, catalog):
            continue
        evidence = {
            "evidence": "pg_constraint",
            "constraint": fk.constraint_name,
            "on_delete": fk.on_delete.value,
            "on_update": fk.on_update.value,
            "deferrable": fk.deferrable,
        }
        edges.append(
            RelationshipEdge(
                edge_id=fk_edge_id(fk, LineageLevel.TABLE),
                level=LineageLevel.TABLE,
                origin=EdgeOrigin.DECLARED_FK,
                source=fk.parent,
                target=fk.child,
                source_columns=tuple(fk.parent_columns),
                target_columns=tuple(fk.child_columns),
                transform=Transform.FOREIGN_KEY,
                traceability=Traceability.KEY_PRESERVING,
                status=EdgeStatus.CONFIRMED,
                on_delete=fk.on_delete,
                constraint_name=fk.constraint_name,
                details=evidence,
            )
        )
        for i, (pc, cc) in enumerate(zip(fk.parent_columns, fk.child_columns, strict=True)):
            edges.append(
                RelationshipEdge(
                    edge_id=fk_edge_id(fk, LineageLevel.COLUMN, i),
                    level=LineageLevel.COLUMN,
                    origin=EdgeOrigin.DECLARED_FK,
                    source=fk.parent,
                    target=fk.child,
                    source_columns=(pc,),
                    target_columns=(cc,),
                    transform=Transform.FOREIGN_KEY,
                    traceability=Traceability.KEY_PRESERVING,
                    status=EdgeStatus.CONFIRMED,
                    on_delete=fk.on_delete,
                    constraint_name=fk.constraint_name,
                    details={**evidence, "pair_index": i, "pair_count": len(fk.child_columns)},
                )
            )
    return edges


def discover_foreign_keys(
    reader: CatalogReader, catalog: CatalogSnapshot
) -> list[RelationshipEdge]:
    """Read declared FKs of the snapshot's schemas and convert them to lineage edges."""
    edges = foreign_key_edges(reader.list_foreign_keys(list(catalog.schemas)), catalog)
    _log.info("declared FK discovery: %d edges", len(edges))
    return edges
