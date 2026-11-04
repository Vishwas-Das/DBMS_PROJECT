"""Mission-brief facade over `LineageGraph` and `RecordGraph`.

Every function here is a thin, documented wrapper around one existing method. No algorithm
lives in this module, so the facade and the class methods always return the same result.
(Not to be confused with `lae.api`, the HTTP layer.)

| Facade                   | Delegates to                                  |
|--------------------------|-----------------------------------------------|
| `build_graph`            | `build_lineage_graph`                         |
| `add_node`               | `LineageGraph.add_table`                      |
| `add_edge`               | `LineageGraph.add_edge`                       |
| `discover_dependents`    | `LineageGraph.downstream`                     |
| `get_descendants`        | `LineageGraph.traverse`                       |
| `detect_cycles`          | `LineageGraph.table_cycles`                   |
| `get_execution_order`    | `RecordGraph.deletion_order`                  |
| `serialize_graph`        | `LineageGraph.to_node_link`                   |
| `serialize_record_graph` | `RecordGraph.to_node_link`                    |
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any
from uuid import UUID

from lae.contracts.enums import EdgeStatus, LineageLevel
from lae.contracts.types import (
    CatalogSnapshot,
    ColumnRef,
    PiiColumn,
    RecordKey,
    RelationshipEdge,
    TableInfo,
    TableRef,
)
from lae.lineage.graph import (
    ALL_STATUSES,
    CONFIRMED_ONLY,
    LineageGraph,
    TraversalOrder,
    build_lineage_graph,
)
from lae.lineage.record_graph import DeletionOrder, RecordGraph

__all__ = [
    "add_edge",
    "add_node",
    "build_graph",
    "detect_cycles",
    "discover_dependents",
    "get_descendants",
    "get_execution_order",
    "serialize_graph",
    "serialize_record_graph",
]


def build_graph(
    catalog: CatalogSnapshot,
    edges: Iterable[RelationshipEdge],
    pii: Mapping[ColumnRef, PiiColumn] | None = None,
    artifacts: Iterable[TableRef] = (),
) -> LineageGraph:
    """Build the table/column lineage graph. Same as `build_lineage_graph`.

    Raises GraphIntegrityError for edges, PII entries or artifacts that reference unknown
    tables/columns, and for duplicate edge ids.
    """
    return build_lineage_graph(catalog, edges, pii, artifacts)


def add_node(
    graph: LineageGraph,
    table_info: TableInfo,
    pii: Mapping[ColumnRef, PiiColumn] | None = None,
    *,
    is_artifact: bool = False,
) -> None:
    """Add a table and its columns to `graph` in place. See `LineageGraph.add_table`.

    Raises GraphIntegrityError(code="DUPLICATE_TABLE") if the table is already present.
    """
    graph.add_table(table_info, pii, is_artifact=is_artifact)


def add_edge(graph: LineageGraph, edge: RelationshipEdge) -> None:
    """Add one lineage edge to `graph` in place, validated against its catalog.

    See `LineageGraph.add_edge`. Raises GraphIntegrityError for a duplicate edge_id or an
    unknown table/column.
    """
    graph.add_edge(edge)


def discover_dependents(
    graph: LineageGraph,
    table: TableRef,
    statuses: frozenset[EdgeStatus] = CONFIRMED_ONLY,
) -> list[RelationshipEdge]:
    """Direct downstream TABLE-level edges of `table` (CONFIRMED only by default).

    Sorted by (target, columns, edge_id). Raises UnknownTableError for a table not in the
    catalog.
    """
    return graph.downstream(table, statuses)


def get_descendants(
    graph: LineageGraph,
    table: TableRef,
    order: TraversalOrder = "bfs",
    statuses: frozenset[EdgeStatus] = CONFIRMED_ONLY,
) -> list[TableRef]:
    """All downstream tables of `table`, excluding it, in deterministic BFS or DFS order.

    Neighbours are visited in TableRef order. See `LineageGraph.traverse`.
    """
    return graph.traverse(table, order, statuses)


def detect_cycles(
    graph: LineageGraph, statuses: frozenset[EdgeStatus] = CONFIRMED_ONLY
) -> list[list[TableRef]]:
    """Table-level simple cycles, each rotated to its smallest table, sorted.

    See `LineageGraph.table_cycles`.
    """
    return graph.table_cycles(statuses)


def get_execution_order(
    record_graph: RecordGraph, nullable: Callable[[UUID], bool]
) -> DeletionOrder:
    """Children-before-parents deletion order with the nullable references to break first.

    See `RecordGraph.deletion_order`. Raises GraphIntegrityError(code="UNBREAKABLE_CYCLE").
    """
    return record_graph.deletion_order(nullable)


def serialize_graph(
    graph: LineageGraph,
    level: LineageLevel,
    statuses: frozenset[EdgeStatus] = ALL_STATUSES,
) -> dict[str, Any]:
    """JSON-able ``{"nodes", "edges"}`` for TABLE or COLUMN level. Every edge has `evidence`.

    All statuses are included by default so the UI can show SUGGESTED edges. See
    `LineageGraph.to_node_link`.
    """
    return graph.to_node_link(level, statuses)


def serialize_record_graph(
    record_graph: RecordGraph, key_hmac: Callable[[RecordKey], str]
) -> dict[str, Any]:
    """JSON-able record graph where rows are identified only by `key_hmac(key)`.

    No raw key values are emitted. See `RecordGraph.to_node_link`.
    """
    return record_graph.to_node_link(key_hmac)
