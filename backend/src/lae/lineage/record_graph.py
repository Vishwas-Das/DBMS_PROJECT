"""Record-level dependency graph (architecture.md §8 and §9, module_contracts.md §6).

Nodes are concrete rows (`RecordKey`); an edge ``parent -> child`` means "child row depends on
parent row" via the lineage edge `edge_id`. Record keys hold raw PK values, so nothing in this
module logs them, puts them in exception details, or serializes them (see `to_node_link`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import networkx as nx

from lae.audit.canonical import canonical_hash
from lae.contracts.enums import RecordRole
from lae.contracts.types import RecordKey, TableRef
from lae.errors import GraphIntegrityError

__all__ = ["DeletionOrder", "RecordAttrs", "RecordGraph"]

# When a row is reached more than once, the most protective role wins.
_ROLE_PRECEDENCE: dict[RecordRole, int] = {
    RecordRole.OWNED: 0,
    RecordRole.SHARED: 1,
    RecordRole.REFERENCING_OTHER_SUBJECT: 2,
    RecordRole.SUBJECT_ROOT: 3,
}


@dataclass(frozen=True, slots=True)
class RecordAttrs:
    role: RecordRole
    depth: int
    via_edge_ids: tuple[UUID, ...]  # sorted by str(edge_id), unique


@dataclass(frozen=True, slots=True)
class DeletionOrder:
    order: tuple[RecordKey, ...]  # children before parents
    break_edges: tuple[tuple[RecordKey, RecordKey, UUID], ...]  # (parent, child, edge_id)


def _merge_ids(ids: Iterable[UUID]) -> tuple[UUID, ...]:
    return tuple(sorted(set(ids), key=str))


def _edge_key(edge: tuple[RecordKey, RecordKey, UUID]) -> tuple[Any, ...]:
    return (edge[0].sort_key(), edge[1].sort_key(), str(edge[2]))


class RecordGraph:
    """Wraps an `nx.DiGraph` of RecordKey nodes.

    Node attribute ``attrs`` holds a `RecordAttrs`; edge attribute ``edge_ids`` holds the set
    of lineage edge ids linking the two rows (two FKs can link the same pair of rows).
    """

    __slots__ = ("graph",)

    def __init__(self) -> None:
        self.graph: nx.DiGraph = nx.DiGraph()

    # ------------------------------------------------------------------ mutation

    def add_root(self, key: RecordKey) -> None:
        """Add `key` as a SUBJECT_ROOT at depth 0 (upgrading it if already present)."""
        if key in self.graph:
            old: RecordAttrs = self.graph.nodes[key]["attrs"]
            self.graph.nodes[key]["attrs"] = RecordAttrs(
                RecordRole.SUBJECT_ROOT, 0, old.via_edge_ids
            )
        else:
            self.graph.add_node(key, attrs=RecordAttrs(RecordRole.SUBJECT_ROOT, 0, ()))

    def add_dependency(
        self, parent: RecordKey, child: RecordKey, edge_id: UUID, role: RecordRole
    ) -> bool:
        """Record that `child` depends on `parent` via lineage edge `edge_id`.

        Returns True if `child` was not in the graph before. For an existing child the
        edge id is accumulated into via_edge_ids, depth becomes min(old, parent depth + 1),
        and the more protective role is kept (SUBJECT_ROOT > REFERENCING_OTHER_SUBJECT >
        SHARED > OWNED).
        """
        if parent not in self.graph:
            raise GraphIntegrityError(
                "parent record is not in the graph",
                code="UNKNOWN_PARENT",
                details={"table": parent.table.qualified(), "edge_id": str(edge_id)},
            )
        if role is RecordRole.SUBJECT_ROOT:
            raise GraphIntegrityError(
                "use add_root for SUBJECT_ROOT records",
                code="INVALID_ROLE",
                details={"table": child.table.qualified()},
            )
        parent_attrs: RecordAttrs = self.graph.nodes[parent]["attrs"]
        depth = parent_attrs.depth + 1
        is_new = child not in self.graph
        if is_new:
            self.graph.add_node(child, attrs=RecordAttrs(role, depth, (edge_id,)))
        else:
            old: RecordAttrs = self.graph.nodes[child]["attrs"]
            keep_role = role if _ROLE_PRECEDENCE[role] > _ROLE_PRECEDENCE[old.role] else old.role
            self.graph.nodes[child]["attrs"] = RecordAttrs(
                keep_role,
                min(old.depth, depth),
                _merge_ids((*old.via_edge_ids, edge_id)),
            )
        if self.graph.has_edge(parent, child):
            self.graph.edges[parent, child]["edge_ids"].add(edge_id)
        else:
            self.graph.add_edge(parent, child, edge_ids={edge_id})
        return is_new

    # ------------------------------------------------------------------ queries

    def __len__(self) -> int:
        return int(self.graph.number_of_nodes())

    def __contains__(self, key: object) -> bool:
        return key in self.graph

    def attrs(self, key: RecordKey) -> RecordAttrs:
        try:
            result: RecordAttrs = self.graph.nodes[key]["attrs"]
        except KeyError:
            raise GraphIntegrityError(
                "record is not in the graph",
                code="UNKNOWN_RECORD",
                details={"table": key.table.qualified()},
            ) from None
        return result

    def _sorted_keys(self) -> list[RecordKey]:
        return sorted(self.graph.nodes, key=RecordKey.sort_key)

    def nodes(self) -> list[tuple[RecordKey, RecordAttrs]]:
        """All records with attributes, sorted by RecordKey.sort_key."""
        return [(k, self.graph.nodes[k]["attrs"]) for k in self._sorted_keys()]

    def edges(self) -> list[tuple[RecordKey, RecordKey, UUID]]:
        """All (parent, child, edge_id) triples, sorted deterministically."""
        out = [
            (u, v, eid) for u, v, data in self.graph.edges(data=True) for eid in data["edge_ids"]
        ]
        return sorted(out, key=_edge_key)

    def by_table(self) -> dict[TableRef, list[RecordKey]]:
        """Records grouped by table; tables and keys sorted."""
        out: dict[TableRef, list[RecordKey]] = {}
        for k in self._sorted_keys():
            out.setdefault(k.table, []).append(k)
        return out

    # ------------------------------------------------------------------ ordering

    def deletion_order(self, nullable: Callable[[UUID], bool]) -> DeletionOrder:
        """Children before parents; cycles broken only through nullable references.

        Self-references of a single row are ignored (one DELETE removes the row and its own
        reference). For each strongly connected component, every intra-component graph edge
        whose edge ids are *all* nullable is removed; if the component is still cyclic,
        GraphIntegrityError(code="UNBREAKABLE_CYCLE") is raised. Removed edges are then
        restored, in deterministic order, whenever restoring keeps the graph acyclic, so
        `break_edges` holds only references that really must be nullified first.
        """
        work = nx.DiGraph()
        work.add_nodes_from(self.graph.nodes)
        work.add_edges_from((u, v) for u, v in self.graph.edges if u != v)

        removed: list[tuple[RecordKey, RecordKey]] = []
        sccs = [c for c in nx.strongly_connected_components(work) if len(c) > 1]
        sccs.sort(key=lambda c: min(k.sort_key() for k in c))
        for comp in sccs:
            intra = sorted(
                ((u, v) for u, v in work.subgraph(comp).edges),
                key=lambda uv: (uv[0].sort_key(), uv[1].sort_key()),
            )
            candidates = [
                (u, v)
                for u, v in intra
                if all(nullable(e) for e in self.graph.edges[u, v]["edge_ids"])
            ]
            work.remove_edges_from(candidates)
            if not nx.is_directed_acyclic_graph(work.subgraph(comp)):
                raise GraphIntegrityError(
                    "record-level cycle cannot be broken by nullifying a reference",
                    code="UNBREAKABLE_CYCLE",
                    details={
                        "tables": sorted({k.table.qualified() for k in comp}),
                        "record_count": len(comp),
                    },
                )
            for u, v in candidates:
                if nx.has_path(work, v, u):
                    removed.append((u, v))
                else:
                    work.add_edge(u, v)

        # Reverse graph (child -> parent): a topological sort yields children first.
        reverse = work.reverse(copy=True)
        order = tuple(nx.lexicographical_topological_sort(reverse, key=RecordKey.sort_key))
        breaks = sorted(
            ((u, v, eid) for u, v in removed for eid in self.graph.edges[u, v]["edge_ids"]),
            key=_edge_key,
        )
        return DeletionOrder(order=order, break_edges=tuple(breaks))

    # ------------------------------------------------------------------ hashing / output

    def record_set_hash(self) -> str:
        """SHA-256 of the canonical JSON list of all record keys, sorted by sort_key."""
        return canonical_hash([k.as_canonical() for k in self._sorted_keys()])

    def to_node_link(self, key_hmac: Callable[[RecordKey], str]) -> dict[str, Any]:
        """JSON-able ``{"nodes", "edges"}`` with records identified only by `key_hmac(key)`.

        Raw key values are never emitted. Nodes are sorted by (table, id); edges by
        (source, target, edge_id).
        """
        ids = {k: key_hmac(k) for k in self.graph.nodes}
        nodes = []
        for k in self.graph.nodes:
            a: RecordAttrs = self.graph.nodes[k]["attrs"]
            nodes.append(
                {
                    "id": ids[k],
                    "table": k.table.qualified(),
                    "key_columns": list(k.columns),
                    "is_predicate": k.is_predicate,
                    "role": a.role.value,
                    "depth": a.depth,
                    "via_edge_ids": [str(e) for e in a.via_edge_ids],
                }
            )
        nodes.sort(key=lambda n: (n["table"], n["id"]))
        edges = [
            {"source": ids[u], "target": ids[v], "edge_id": str(eid)}
            for u, v, data in self.graph.edges(data=True)
            for eid in data["edge_ids"]
        ]
        edges.sort(key=lambda d: (d["source"], d["target"], d["edge_id"]))
        return {"nodes": nodes, "edges": edges}
