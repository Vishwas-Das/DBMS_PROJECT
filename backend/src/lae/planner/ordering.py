"""Deletion ordering: children before parents, cascades made explicit (architecture.md §10.3-4).

Only *explicitly deleted* rows get a DELETE statement. A row removed by ``ON DELETE CASCADE``
(an EXPECT_CASCADE row) disappears together with its *trigger*: the explicitly deleted
ancestor whose DELETE cascades down to it. A constraint "row v must be gone before row u is
deleted" is therefore mapped onto the triggers of u and v before ordering:

- every trigger of u must run after the designated trigger of v (the smallest one);
- if both triggers are the same row, or two rows of the same table, one DELETE statement
  removes both, and PostgreSQL checks RESTRICT / NO ACTION at the end of the statement.

The mapped graph is ordered with `RecordGraph.deletion_order`, which breaks cycles only
through nullable references. Broken references are returned as the *original*
``(parent, child, edge_id)`` triples, so that the planner can emit NULLIFY_REFERENCE on the
real child row.
"""

from __future__ import annotations

import heapq
from collections.abc import Callable, Mapping, Sequence, Set
from dataclasses import dataclass
from uuid import UUID

from lae.contracts.enums import RecordRole
from lae.contracts.types import RecordKey, TableRef
from lae.errors import GraphIntegrityError
from lae.lineage import RecordGraph

__all__ = ["OrderedDeletes", "order_deletions"]

Triple = tuple[RecordKey, RecordKey, UUID]


@dataclass(frozen=True, slots=True)
class OrderedDeletes:
    batches: tuple[tuple[RecordKey, ...], ...]
    """Explicitly deleted rows, one batch per DELETE statement group, children first."""
    breaks: tuple[Triple, ...]
    """Original (parent, child, edge_id) references that must be nullified before deleting."""
    before: tuple[tuple[RecordKey, RecordKey], ...]
    """(first, then) pairs among explicit rows that the order respects (used for depends_on)."""
    cycle: GraphIntegrityError | None
    """Set when the rows contain a cycle that no nullable reference can break."""


def _sk(k: RecordKey) -> tuple[object, ...]:
    return k.sort_key()


def _triple_key(t: Triple) -> tuple[object, ...]:
    return (t[0].sort_key(), t[1].sort_key(), str(t[2]))


def _topo_tables(tables: Set[TableRef], pairs: Set[tuple[TableRef, TableRef]]) -> list[TableRef]:
    """Kahn's algorithm, smallest TableRef first. Returns fewer tables than given on a cycle."""
    succ: dict[TableRef, set[TableRef]] = {t: set() for t in tables}
    indeg: dict[TableRef, int] = dict.fromkeys(tables, 0)
    for a, b in pairs:
        if b not in succ[a]:
            succ[a].add(b)
            indeg[b] += 1
    heap = [t for t in tables if indeg[t] == 0]
    heapq.heapify(heap)
    out: list[TableRef] = []
    while heap:
        t = heapq.heappop(heap)
        out.append(t)
        for b in sorted(succ[t]):
            indeg[b] -= 1
            if indeg[b] == 0:
                heapq.heappush(heap, b)
    return out


def _group_by_table(
    rows: Sequence[RecordKey], tables: Sequence[TableRef]
) -> list[tuple[RecordKey, ...]]:
    by_table: dict[TableRef, list[RecordKey]] = {}
    for r in rows:
        by_table.setdefault(r.table, []).append(r)
    return [tuple(sorted(by_table[t], key=_sk)) for t in tables if t in by_table]


def order_deletions(
    explicit: Set[RecordKey],
    triggers: Mapping[RecordKey, frozenset[RecordKey]],
    constraints: Sequence[Triple],
    nullable: Callable[[UUID], bool],
) -> OrderedDeletes:
    """Order the explicit deletes.

    `constraints` holds original ``(u, v, edge_id)`` triples meaning "v must be removed before
    u is removed" (u and v are explicit or EXPECT_CASCADE rows). `triggers[r]` is the set of
    explicit rows whose DELETE removes `r` (``{r}`` for an explicit row). `nullable(edge_id)`
    says whether the reference may be nullified to break a cycle.
    """
    mapped: dict[Triple, list[tuple[RecordKey, RecordKey]]] = {}
    for u, v, eid in constraints:
        d = min(triggers[v], key=_sk)
        for b in sorted(triggers[u], key=_sk):
            if b == d or b.table == d.table:
                continue
            mapped.setdefault((b, d, eid), []).append((u, v))

    sub = RecordGraph()
    for r in sorted(explicit, key=_sk):
        sub.add_root(r)
    for b, d, eid in sorted(mapped, key=_triple_key):
        sub.add_dependency(b, d, eid, RecordRole.OWNED)

    all_tables = {r.table for r in explicit}
    try:
        order = sub.deletion_order(nullable)
    except GraphIntegrityError as exc:
        if exc.code != "UNBREAKABLE_CYCLE":  # pragma: no cover - deletion_order raises nothing else
            raise
        rows = sorted(explicit, key=_sk)
        return OrderedDeletes(
            batches=tuple(_group_by_table(rows, sorted(all_tables))),
            breaks=(),
            before=tuple(
                sorted(((d, b) for b, d, _ in mapped), key=lambda p: (_sk(p[0]), _sk(p[1])))
            ),
            cycle=exc,
        )

    broken = {(b, d) for b, d, _ in order.break_edges}
    breaks = sorted(
        {(u, v, eid) for b, d, eid in order.break_edges for u, v in mapped[(b, d, eid)]},
        key=_triple_key,
    )
    before = {(d, b) for b, d, _ in mapped if (b, d) not in broken}
    table_pairs = {(f.table, t.table) for f, t in before if f.table != t.table}
    topo = _topo_tables(all_tables, table_pairs)
    if len(topo) == len(all_tables):
        batches = _group_by_table(list(order.order), topo)
    else:
        # Table-level cycle without a record-level one: split tables into per-rank batches.
        firsts: dict[RecordKey, list[RecordKey]] = {}
        for f, t in before:
            firsts.setdefault(t, []).append(f)
        rank: dict[RecordKey, int] = {}
        for r in order.order:  # children (firsts) come before the rows that depend on them
            rank[r] = 1 + max((rank[f] for f in firsts.get(r, ())), default=-1)
        groups: dict[tuple[int, TableRef], list[RecordKey]] = {}
        for r in order.order:
            groups.setdefault((rank[r], r.table), []).append(r)
        batches = [tuple(sorted(groups[g], key=_sk)) for g in sorted(groups)]
    return OrderedDeletes(
        batches=tuple(batches),
        breaks=tuple(breaks),
        before=tuple(sorted(before, key=lambda p: (_sk(p[0]), _sk(p[1])))),
        cycle=None,
    )
