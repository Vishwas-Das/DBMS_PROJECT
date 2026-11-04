"""Hypothesis property tests for RecordGraph / LineageGraph ordering and cycle handling."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import networkx as nx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from lae.contracts.enums import (
    EdgeOrigin,
    EdgeStatus,
    LineageLevel,
    RecordRole,
    TableKind,
    Traceability,
    Transform,
)
from lae.contracts.types import (
    CatalogSnapshot,
    ColumnInfo,
    RecordKey,
    RelationshipEdge,
    TableInfo,
    TableRef,
)
from lae.errors import GraphIntegrityError
from lae.lineage import RecordGraph, build_lineage_graph

pytestmark = pytest.mark.unit

T = TableRef("app", "node")


def key(i: int) -> RecordKey:
    return RecordKey(T, ("id",), (i,))


def eid(i: int) -> UUID:
    return UUID(int=i + 1)


@st.composite
def dags(draw: st.DrawFn) -> tuple[int, list[tuple[int, int]]]:
    """Random DAG on nodes 0..n-1; edges only go from lower to higher index (parent -> child)."""
    n = draw(st.integers(min_value=1, max_value=25))
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    edges = draw(st.lists(st.sampled_from(pairs), unique=True, max_size=60)) if pairs else []
    return n, edges


@st.composite
def digraphs(draw: st.DrawFn) -> tuple[int, list[tuple[int, int]], set[int]]:
    """Random directed graph (cycles allowed, no self-loops) plus a set of nullable edge ids."""
    n = draw(st.integers(min_value=1, max_value=12))
    pairs = [(i, j) for i in range(n) for j in range(n) if i != j]
    edges = draw(st.lists(st.sampled_from(pairs), unique=True, max_size=30)) if pairs else []
    nullable = draw(st.sets(st.integers(min_value=0, max_value=max(len(edges) - 1, 0))))
    return n, edges, nullable


def build_record_graph(n: int, edges: list[tuple[int, int]]) -> RecordGraph:
    rg = RecordGraph()
    for i in range(n):  # every node is a root so any edge can be added in any order
        rg.add_root(key(i))
    for idx, (u, v) in enumerate(edges):
        rg.add_dependency(key(u), key(v), eid(idx), RecordRole.OWNED)
    return rg


@settings(max_examples=150, deadline=None)
@given(dags())
def test_dag_deletion_order_respects_every_edge(dag: tuple[int, list[tuple[int, int]]]) -> None:
    n, edges = dag
    rg = build_record_graph(n, edges)
    d = rg.deletion_order(lambda _e: False)
    assert d.break_edges == ()
    assert sorted(d.order, key=RecordKey.sort_key) == [key(i) for i in range(n)]
    pos = {k: i for i, k in enumerate(d.order)}
    for u, v in edges:
        assert pos[key(v)] < pos[key(u)]  # child before parent


@settings(max_examples=100, deadline=None)
@given(dags(), st.randoms(use_true_random=False))
def test_hash_and_order_independent_of_insertion_order(dag: Any, rnd: Any) -> None:
    n, edges = dag
    shuffled = list(enumerate(edges))
    rnd.shuffle(shuffled)
    a = build_record_graph(n, edges)
    b = RecordGraph()
    roots = list(range(n))
    rnd.shuffle(roots)
    for i in roots:
        b.add_root(key(i))
    for idx, (u, v) in shuffled:
        b.add_dependency(key(u), key(v), eid(idx), RecordRole.OWNED)
    assert a.record_set_hash() == b.record_set_hash()
    assert a.deletion_order(lambda _e: False) == b.deletion_order(lambda _e: False)
    assert a.edges() == b.edges()


@settings(max_examples=150, deadline=None)
@given(digraphs())
def test_cycle_breaking_is_correct(case: tuple[int, list[tuple[int, int]], set[int]]) -> None:
    n, edges, nullable_idx = case
    rg = build_record_graph(n, edges)
    nullable_ids = {eid(i) for i in nullable_idx}
    ref = nx.DiGraph()
    ref.add_nodes_from(range(n))
    ref.add_edges_from((u, v) for i, (u, v) in enumerate(edges) if i not in nullable_idx)
    breakable = nx.is_directed_acyclic_graph(ref)
    try:
        d = rg.deletion_order(lambda e: e in nullable_ids)
    except GraphIntegrityError as err:
        assert err.code == "UNBREAKABLE_CYCLE"
        assert not breakable
        return
    assert breakable
    broken = {(p, c, e) for p, c, e in d.break_edges}
    assert all(e in nullable_ids for _, _, e in broken)
    pos = {k: i for i, k in enumerate(d.order)}
    assert len(pos) == n
    for i, (u, v) in enumerate(edges):
        if (key(u), key(v), eid(i)) not in broken:
            assert pos[key(v)] < pos[key(u)]
    if nx.is_directed_acyclic_graph(nx.DiGraph(edges)):
        assert d.break_edges == ()


def _catalog(n: int) -> CatalogSnapshot:
    tables = {
        TableRef("app", f"t{i:02d}"): TableInfo(
            TableRef("app", f"t{i:02d}"),
            TableKind.TABLE,
            (ColumnInfo("id", 1, "integer", False),),
            ("id",),
        )
        for i in range(n)
    }
    return CatalogSnapshot(tables, ("app",), "0" * 64)


@settings(max_examples=100, deadline=None)
@given(
    st.integers(min_value=1, max_value=8).flatmap(
        lambda n: st.tuples(
            st.just(n),
            st.lists(st.tuples(st.integers(0, n - 1), st.integers(0, n - 1)), max_size=20),
        )
    )
)
def test_table_cycle_detection_matches_networkx(case: tuple[int, list[tuple[int, int]]]) -> None:
    n, pairs = case
    cat = _catalog(n)
    refs = sorted(cat.tables)
    edges = [
        RelationshipEdge(
            edge_id=UUID(int=i + 1),
            level=LineageLevel.TABLE,
            origin=EdgeOrigin.DECLARED_FK,
            source=refs[u],
            target=refs[v],
            source_columns=(),
            target_columns=(),
            transform=Transform.FOREIGN_KEY,
            traceability=Traceability.KEY_PRESERVING,
            status=EdgeStatus.CONFIRMED,
        )
        for i, (u, v) in enumerate(pairs)
    ]
    g = build_lineage_graph(cat, edges)
    cycles = g.table_cycles()
    ref = nx.DiGraph()
    ref.add_nodes_from(refs)
    ref.add_edges_from((refs[u], refs[v]) for u, v in pairs)
    assert (len(cycles) > 0) == (not nx.is_directed_acyclic_graph(ref))
    assert cycles == sorted(cycles)
    for c in cycles:
        for a, b in zip(c, c[1:] + c[:1], strict=True):
            assert ref.has_edge(a, b)
    # artifact_order over all tables succeeds iff the graph is acyclic
    try:
        order = g.artifact_order(refs)
    except GraphIntegrityError:
        assert cycles
    else:
        assert not cycles
        pos = {t: i for i, t in enumerate(order)}
        for u, v in pairs:
            assert pos[refs[u]] < pos[refs[v]]
