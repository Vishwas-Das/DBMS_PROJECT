"""Table- and column-level lineage graph (architecture.md §9, module_contracts.md §6).

A table- or column-level edge says a dependency is *possible*; it never implies affected
records (architecture.md §1.1). Record-level dependencies live in `lae.lineage.record_graph`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

import networkx as nx

from lae.contracts.enums import EdgeStatus, LineageLevel, TableKind
from lae.contracts.types import (
    CatalogSnapshot,
    ColumnRef,
    PiiColumn,
    RelationshipEdge,
    TableInfo,
    TableRef,
)
from lae.errors import GraphIntegrityError

__all__ = [
    "CONFIRMED_ONLY",
    "LineageGraph",
    "build_lineage_graph",
    "column_node_id",
    "table_node_id",
]

CONFIRMED_ONLY: frozenset[EdgeStatus] = frozenset({EdgeStatus.CONFIRMED})
_ALL_STATUSES: frozenset[EdgeStatus] = frozenset(EdgeStatus)
_CONTAINS = "contains"
_ARTIFACT_KINDS = frozenset({TableKind.VIEW, TableKind.MATERIALIZED_VIEW})


def table_node_id(table: TableRef) -> str:
    return f"t:{table.qualified()}"


def column_node_id(column: ColumnRef) -> str:
    return f"c:{column.qualified()}"


def _edge_sort_key(edge: RelationshipEdge, by: TableRef) -> tuple[Any, ...]:
    return (by, edge.source_columns, edge.target_columns, str(edge.edge_id))


def _canonical_cycle(cycle: list[TableRef]) -> list[TableRef]:
    i = cycle.index(min(cycle))
    return cycle[i:] + cycle[:i]


class LineageGraph:
    """Wraps an `nx.MultiDiGraph` holding table nodes, column nodes and lineage edges.

    Node keys: ``t:schema.table`` and ``c:schema.table.column``. TABLE-level edges link table
    nodes; COLUMN-level edges link column nodes (one nx edge per column pair). Every lineage
    edge carries the full `RelationshipEdge` in its ``edge`` attribute. Table → column
    containment edges carry ``contains=True`` and no ``edge``.
    """

    __slots__ = ("_by_id", "_column_edges", "_table_edges", "catalog", "graph")

    def __init__(
        self,
        catalog: CatalogSnapshot,
        graph: nx.MultiDiGraph,
        edges: Iterable[RelationshipEdge],
    ) -> None:
        self.catalog = catalog
        self.graph = graph
        ordered = sorted(edges, key=lambda e: str(e.edge_id))
        self._by_id: dict[UUID, RelationshipEdge] = {e.edge_id: e for e in ordered}
        self._table_edges: tuple[RelationshipEdge, ...] = tuple(
            e for e in ordered if e.level is LineageLevel.TABLE
        )
        self._column_edges: tuple[RelationshipEdge, ...] = tuple(
            e for e in ordered if e.level is LineageLevel.COLUMN
        )

    # ------------------------------------------------------------------ basic accessors

    def edge(self, edge_id: UUID) -> RelationshipEdge:
        try:
            return self._by_id[edge_id]
        except KeyError:
            raise GraphIntegrityError(
                "unknown lineage edge", code="UNKNOWN_EDGE", details={"edge_id": str(edge_id)}
            ) from None

    def edges(self, statuses: frozenset[EdgeStatus] = _ALL_STATUSES) -> list[RelationshipEdge]:
        """All lineage edges with a status in `statuses`, sorted by edge_id."""
        return [e for e in self._by_id.values() if e.status in statuses]

    def _check_table(self, table: TableRef) -> TableInfo:
        return self.catalog.table(table)  # raises UnknownTableError

    # ------------------------------------------------------------------ views

    def _add_table_nodes(self, g: nx.MultiDiGraph) -> None:
        for ref in sorted(self.catalog.tables):
            node = table_node_id(ref)
            g.add_node(node, **self.graph.nodes[node])

    def table_view(self, statuses: frozenset[EdgeStatus] = CONFIRMED_ONLY) -> nx.MultiDiGraph:
        """A fresh MultiDiGraph: all table nodes + TABLE-level edges with a status in `statuses`."""
        g = nx.MultiDiGraph()
        self._add_table_nodes(g)
        for e in self._table_edges:
            if e.status in statuses:
                g.add_edge(
                    table_node_id(e.source), table_node_id(e.target), key=str(e.edge_id), edge=e
                )
        return g

    def column_view(self, statuses: frozenset[EdgeStatus] = CONFIRMED_ONLY) -> nx.MultiDiGraph:
        """Table + column nodes, containment edges, and COLUMN-level edges in `statuses`."""
        g = nx.MultiDiGraph()
        self._add_table_nodes(g)
        for ref in sorted(self.catalog.tables):
            tnode = table_node_id(ref)
            for col in self.catalog.tables[ref].columns:
                cnode = column_node_id(ColumnRef(ref, col.name))
                g.add_node(cnode, **self.graph.nodes[cnode])
                g.add_edge(tnode, cnode, key=_CONTAINS, contains=True)
        for e in self._column_edges:
            if e.status in statuses:
                for i, (sc, tc) in enumerate(zip(e.source_columns, e.target_columns, strict=True)):
                    g.add_edge(
                        column_node_id(ColumnRef(e.source, sc)),
                        column_node_id(ColumnRef(e.target, tc)),
                        key=f"{e.edge_id}:{i}",
                        edge=e,
                    )
        return g

    def _table_digraph(self, statuses: frozenset[EdgeStatus]) -> nx.DiGraph:
        """Simple DiGraph over TableRef nodes (parallel edges collapsed)."""
        g = nx.DiGraph()
        g.add_nodes_from(sorted(self.catalog.tables))
        for e in self._table_edges:
            if e.status in statuses:
                g.add_edge(e.source, e.target)
        return g

    # ------------------------------------------------------------------ traversal

    def downstream(
        self, table: TableRef, statuses: frozenset[EdgeStatus] = CONFIRMED_ONLY
    ) -> list[RelationshipEdge]:
        """TABLE-level edges leaving `table`, sorted by (target, columns, edge_id)."""
        self._check_table(table)
        out = [e for e in self._table_edges if e.source == table and e.status in statuses]
        return sorted(out, key=lambda e: _edge_sort_key(e, e.target))

    def upstream(
        self, table: TableRef, statuses: frozenset[EdgeStatus] = CONFIRMED_ONLY
    ) -> list[RelationshipEdge]:
        """TABLE-level edges entering `table`, sorted by (source, columns, edge_id)."""
        self._check_table(table)
        out = [e for e in self._table_edges if e.target == table and e.status in statuses]
        return sorted(out, key=lambda e: _edge_sort_key(e, e.source))

    def column_edges_for(self, table_edge: RelationshipEdge) -> list[RelationshipEdge]:
        """COLUMN-level edges belonging to `table_edge`.

        Same source/target tables and the same constraint_name / artifact_id. Sorted by
        (source_columns, target_columns, edge_id).
        """
        if table_edge.level is not LineageLevel.TABLE:
            raise GraphIntegrityError(
                "column_edges_for expects a TABLE-level edge",
                code="NOT_A_TABLE_EDGE",
                details={"edge_id": str(table_edge.edge_id)},
            )
        out = [
            e
            for e in self._column_edges
            if e.source == table_edge.source
            and e.target == table_edge.target
            and e.constraint_name == table_edge.constraint_name
            and e.artifact_id == table_edge.artifact_id
        ]
        return sorted(out, key=lambda e: _edge_sort_key(e, e.source))

    def reachable_from(
        self, table: TableRef, statuses: frozenset[EdgeStatus] = CONFIRMED_ONLY
    ) -> set[TableRef]:
        """Tables reachable downstream from `table` (excluding `table` itself)."""
        self._check_table(table)
        return set(nx.descendants(self._table_digraph(statuses), table))

    def table_cycles(
        self, statuses: frozenset[EdgeStatus] = CONFIRMED_ONLY
    ) -> list[list[TableRef]]:
        """Simple cycles of the table view (self-loops included), in a deterministic order.

        Each cycle is rotated to start at its smallest TableRef; the list is sorted.
        """
        g = self._table_digraph(statuses)
        cycles = [_canonical_cycle(list(c)) for c in nx.simple_cycles(g)]
        return sorted(cycles)

    def artifact_order(self, artifacts: Iterable[TableRef]) -> list[TableRef]:
        """Topological order of `artifacts` for refresh (upstream artifacts first).

        `b` must come after `a` if `b` is reachable from `a` over CONFIRMED TABLE edges.
        Ties are broken by TableRef order. Raises GraphIntegrityError if the artifacts
        depend on each other cyclically (including an artifact that reaches itself).
        """
        wanted = sorted(set(artifacts))
        for a in wanted:
            self._check_table(a)
        full = self._table_digraph(CONFIRMED_ONLY)
        order_graph = nx.DiGraph()
        order_graph.add_nodes_from(wanted)
        wanted_set = set(wanted)
        for a in wanted:
            reach = nx.descendants(full, a)
            if any(s == a or nx.has_path(full, s, a) for s in full.successors(a)):
                raise GraphIntegrityError(
                    "artifact depends on itself",
                    code="ARTIFACT_CYCLE",
                    details={"tables": [a.qualified()]},
                )
            for b in sorted(reach & wanted_set):
                order_graph.add_edge(a, b)
        try:
            return list(
                nx.lexicographical_topological_sort(order_graph, key=lambda t: (t.schema, t.name))
            )
        except nx.NetworkXUnfeasible:  # pragma: no cover - guarded by the self-reach check
            raise GraphIntegrityError(
                "artifact dependency cycle",
                code="ARTIFACT_CYCLE",
                details={"tables": [t.qualified() for t in wanted]},
            ) from None

    # ------------------------------------------------------------------ serialization

    def to_node_link(
        self, level: LineageLevel, statuses: frozenset[EdgeStatus] = _ALL_STATUSES
    ) -> dict[str, Any]:
        """Plain JSON-able ``{"nodes": [...], "edges": [...]}`` (GraphNodeOut / GraphEdgeOut).

        TABLE: table nodes + TABLE-level edges. COLUMN: table and column nodes + COLUMN-level
        edges, one entry per column pair. All statuses are included by default so the UI can
        show SUGGESTED edges for review. Nodes are sorted by id; edges by (id, source, target).
        """
        if level is LineageLevel.TABLE:
            node_ids = [table_node_id(t) for t in self.catalog.tables]
            lineage_edges = self._table_edges
        elif level is LineageLevel.COLUMN:
            node_ids = [table_node_id(t) for t in self.catalog.tables] + [
                column_node_id(ColumnRef(t, c.name))
                for t, info in self.catalog.tables.items()
                for c in info.columns
            ]
            lineage_edges = self._column_edges
        else:
            raise GraphIntegrityError(
                "node-link output exists only for TABLE and COLUMN levels",
                code="INVALID_LEVEL",
                details={"level": str(level)},
            )
        nodes = [self._node_out(n) for n in sorted(node_ids)]
        edges: list[dict[str, Any]] = []
        for e in lineage_edges:
            if e.status not in statuses:
                continue
            if level is LineageLevel.TABLE:
                endpoints = [(table_node_id(e.source), table_node_id(e.target))]
            else:
                endpoints = [
                    (
                        column_node_id(ColumnRef(e.source, sc)),
                        column_node_id(ColumnRef(e.target, tc)),
                    )
                    for sc, tc in zip(e.source_columns, e.target_columns, strict=True)
                ]
            for src, tgt in endpoints:
                edges.append(_edge_out(e, src, tgt))
        edges.sort(key=lambda d: (d["id"], d["source"], d["target"]))
        return {"nodes": nodes, "edges": edges}

    def _node_out(self, node_id: str) -> dict[str, Any]:
        a = self.graph.nodes[node_id]
        table: TableRef = a["table"]
        return {
            "id": node_id,
            "kind": a["kind"],
            "schema_name": table.schema,
            "table_name": table.name,
            "column_name": a.get("column"),
            "table_kind": a["table_kind"].value if a["kind"] == "table" else None,
            "pii_category": a["pii_category"].value if a.get("pii_category") else None,
            "is_artifact": a["is_artifact"],
        }


def _edge_out(e: RelationshipEdge, source: str, target: str) -> dict[str, Any]:
    return {
        "id": str(e.edge_id),
        "source": source,
        "target": target,
        "level": e.level.value,
        "origin": e.origin.value,
        "transform": e.transform.value,
        "traceability": e.traceability.value,
        "status": e.status.value,
        "source_columns": list(e.source_columns),
        "target_columns": list(e.target_columns),
        "on_delete_action": e.on_delete.value if e.on_delete is not None else None,
        "confidence": e.confidence,
    }


# ---------------------------------------------------------------------- construction


def _integrity(message: str, **details: Any) -> GraphIntegrityError:
    return GraphIntegrityError(message, details=details)


def _validate_edge(catalog: CatalogSnapshot, e: RelationshipEdge) -> None:
    for side, ref, cols in (
        ("source", e.source, e.source_columns),
        ("target", e.target, e.target_columns),
    ):
        if not catalog.has_table(ref):
            raise _integrity(
                f"edge references unknown {side} table {ref.qualified()}",
                edge_id=str(e.edge_id),
                table=ref.qualified(),
            )
        info = catalog.tables[ref]
        for col in cols:
            if not info.has_column(col):
                raise _integrity(
                    f"edge references unknown {side} column {ref.qualified()}.{col}",
                    edge_id=str(e.edge_id),
                    table=ref.qualified(),
                    column=col,
                )


def build_lineage_graph(
    catalog: CatalogSnapshot,
    edges: Iterable[RelationshipEdge],
    pii: Mapping[ColumnRef, PiiColumn] | None = None,
) -> LineageGraph:
    """Build a `LineageGraph` from a catalog snapshot and lineage edges.

    Raises GraphIntegrityError if an edge (or PII entry) references a table/column not in
    the catalog, or if two edges share an edge_id.
    """
    pii = pii or {}
    edge_list = list(edges)
    seen: set[UUID] = set()
    for e in edge_list:
        if e.edge_id in seen:
            raise GraphIntegrityError(
                "duplicate edge_id", code="DUPLICATE_EDGE", details={"edge_id": str(e.edge_id)}
            )
        seen.add(e.edge_id)
        _validate_edge(catalog, e)
    for col_ref in pii:
        if not catalog.has_table(col_ref.table) or not catalog.tables[col_ref.table].has_column(
            col_ref.column
        ):
            raise _integrity(
                f"PII classification references unknown column {col_ref.qualified()}",
                table=col_ref.table.qualified(),
                column=col_ref.column,
            )

    artifact_tables = {e.target for e in edge_list if e.artifact_id is not None}
    g = nx.MultiDiGraph()
    for ref in sorted(catalog.tables):
        info = catalog.tables[ref]
        is_artifact = info.kind in _ARTIFACT_KINDS or ref in artifact_tables
        tnode = table_node_id(ref)
        g.add_node(
            tnode,
            kind="table",
            table=ref,
            table_kind=info.kind,
            pk=info.primary_key,
            is_artifact=is_artifact,
        )
        for col in info.columns:
            cref = ColumnRef(ref, col.name)
            pc = pii.get(cref)
            cnode = column_node_id(cref)
            g.add_node(
                cnode,
                kind="column",
                table=ref,
                column=col.name,
                table_kind=info.kind,
                data_type=col.data_type,
                is_nullable=col.is_nullable,
                pii_category=pc.category if pc is not None else None,
                is_artifact=is_artifact,
            )
            g.add_edge(tnode, cnode, key=_CONTAINS, contains=True)
    for e in sorted(edge_list, key=lambda x: str(x.edge_id)):
        if e.level is LineageLevel.TABLE:
            g.add_edge(table_node_id(e.source), table_node_id(e.target), key=str(e.edge_id), edge=e)
        else:
            for i, (sc, tc) in enumerate(zip(e.source_columns, e.target_columns, strict=True)):
                g.add_edge(
                    column_node_id(ColumnRef(e.source, sc)),
                    column_node_id(ColumnRef(e.target, tc)),
                    key=f"{e.edge_id}:{i}",
                    edge=e,
                )
    return LineageGraph(catalog, g, edge_list)
