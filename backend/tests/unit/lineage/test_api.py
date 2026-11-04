"""Facade (lae.lineage.api), incremental mutation, traversal order, artifacts and evidence."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from lae.contracts.enums import (
    EdgeOrigin,
    EdgeStatus,
    ErasureStrategy,
    LineageLevel,
    PiiCategory,
    RecordRole,
    TableKind,
)
from lae.contracts.types import (
    CatalogSnapshot,
    ColumnInfo,
    ColumnRef,
    PiiColumn,
    RecordKey,
    TableInfo,
    TableRef,
)
from lae.errors import GraphIntegrityError, UnknownTableError
from lae.lineage import (
    ALL_STATUSES,
    CONFIRMED_ONLY,
    LineageGraph,
    RecordGraph,
    add_edge,
    add_node,
    build_graph,
    build_lineage_graph,
    detect_cycles,
    discover_dependents,
    edge_evidence,
    get_descendants,
    get_execution_order,
    serialize_graph,
    serialize_record_graph,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def g(shop: Any) -> LineageGraph:
    return build_graph(shop.catalog, shop.edges)


def _info(kit: Any, name: str, kind: TableKind = TableKind.TABLE) -> TableInfo:
    return TableInfo(
        kit.t(name),
        kind,
        (ColumnInfo("id", 1, "integer", False), ColumnInfo("customer_id", 2, "integer", True)),
        ("id",),
    )


# ---------------------------------------------------------------------- facade parity


def test_build_graph_matches_build_lineage_graph(shop: Any, g: LineageGraph) -> None:
    ref = build_lineage_graph(shop.catalog, shop.edges)
    for level in (LineageLevel.TABLE, LineageLevel.COLUMN):
        assert serialize_graph(g, level) == ref.to_node_link(level)
        assert serialize_graph(g, level, CONFIRMED_ONLY) == ref.to_node_link(level, CONFIRMED_ONLY)
    assert g.edges() == ref.edges()


def test_query_facades_match_methods(g: LineageGraph, kit: Any) -> None:
    for name in ("customer", "purchase_order", "product", "customer_segment"):
        tbl = kit.t(name)
        assert discover_dependents(g, tbl) == g.downstream(tbl)
        assert discover_dependents(g, tbl, ALL_STATUSES) == g.downstream(tbl, ALL_STATUSES)
        assert get_descendants(g, tbl) == g.traverse(tbl, "bfs")
        assert get_descendants(g, tbl, "dfs") == g.traverse(tbl, "dfs")
    assert detect_cycles(g) == g.table_cycles()
    assert detect_cycles(g, ALL_STATUSES) == g.table_cycles(ALL_STATUSES)


def test_discover_dependents_is_direct_and_confirmed(g: LineageGraph, kit: Any) -> None:
    targets = [e.target for e in discover_dependents(g, kit.t("customer"))]
    assert targets == [kit.t("customer"), kit.t("customer_ltv"), kit.t("purchase_order")]
    assert kit.t("order_line") not in targets  # grandchild, not direct
    assert kit.t("newsletter") not in targets  # SUGGESTED edge
    with_suggested = [e.target for e in discover_dependents(g, kit.t("customer"), ALL_STATUSES)]
    assert kit.t("newsletter") in with_suggested
    with pytest.raises(UnknownTableError):
        discover_dependents(g, kit.t("nope"))


def test_record_facades_match_methods() -> None:
    t = TableRef("app", "customer")
    c = TableRef("app", "purchase_order")
    root = RecordKey(t, ("id",), (42,))
    child = RecordKey(c, ("id",), (1,))
    rg = RecordGraph()
    rg.add_root(root)
    fk = UUID(int=7)
    rg.add_dependency(root, child, fk, RecordRole.OWNED)
    order = get_execution_order(rg, lambda _e: False)
    assert order == rg.deletion_order(lambda _e: False)
    assert order.order == (child, root)

    def fake_hmac(k: RecordKey) -> str:
        return f"h{k.sort_key()[2]!r}"

    out = serialize_record_graph(rg, fake_hmac)
    assert out == rg.to_node_link(fake_hmac)
    assert out["edges"] == [
        {"source": fake_hmac(root), "target": fake_hmac(child), "edge_id": str(fk)}
    ]


# ---------------------------------------------------------------------- traversal order


def test_bfs_and_dfs_order_differ_and_are_deterministic(g: LineageGraph, kit: Any) -> None:
    c = kit.t
    bfs = get_descendants(g, c("customer"), "bfs")
    dfs = get_descendants(g, c("customer"), "dfs")
    assert bfs == [c("customer_ltv"), c("purchase_order"), c("customer_segment"), c("order_line")]
    assert dfs == [c("customer_ltv"), c("customer_segment"), c("purchase_order"), c("order_line")]
    assert set(bfs) == set(dfs) == g.reachable_from(c("customer"))
    assert c("customer") not in bfs  # the start is excluded even though it has a self-loop
    for _ in range(3):
        assert get_descendants(g, c("customer"), "bfs") == bfs
        assert get_descendants(g, c("customer"), "dfs") == dfs


def test_traversal_statuses_and_leaf(g: LineageGraph, kit: Any) -> None:
    c = kit.t
    assert get_descendants(g, c("customer"), "bfs", ALL_STATUSES) == [
        c("customer_ltv"),
        c("newsletter"),
        c("purchase_order"),
        c("customer_segment"),
        c("order_line"),
    ]
    assert get_descendants(g, c("customer_segment")) == []
    assert get_descendants(g, c("newsletter"), "dfs") == []


def test_traversal_independent_of_edge_input_order(shop: Any, kit: Any) -> None:
    a = build_graph(shop.catalog, shop.edges)
    b = build_graph(shop.catalog, list(reversed(shop.edges)))
    for order in ("bfs", "dfs"):
        assert get_descendants(a, kit.t("customer"), order) == get_descendants(  # type: ignore[arg-type]
            b,
            kit.t("customer"),
            order,  # type: ignore[arg-type]
        )
    assert a.edges() == b.edges()


def test_traversal_errors(g: LineageGraph, kit: Any) -> None:
    with pytest.raises(GraphIntegrityError) as ei:
        get_descendants(g, kit.t("customer"), "topo")  # type: ignore[arg-type]
    assert ei.value.code == "INVALID_ORDER"
    with pytest.raises(UnknownTableError):
        get_descendants(g, kit.t("nope"))


# ---------------------------------------------------------------------- incremental mutation


def test_incremental_build_equals_batch_build(shop: Any) -> None:
    """add_node / add_edge from an empty graph give exactly the batch result."""
    empty = CatalogSnapshot({}, ("app",), shop.catalog.fingerprint)
    inc = build_graph(empty, [])
    for ref in sorted(shop.catalog.tables, reverse=True):
        add_node(inc, shop.catalog.tables[ref])
    for e in reversed(shop.edges):
        add_edge(inc, e)
    batch = build_graph(shop.catalog, shop.edges)
    for level in (LineageLevel.TABLE, LineageLevel.COLUMN):
        assert serialize_graph(inc, level) == serialize_graph(batch, level)
    assert inc.edges() == batch.edges()
    assert inc.catalog.fingerprint == shop.catalog.fingerprint
    assert set(inc.catalog.tables) == set(shop.catalog.tables)
    assert inc.table_cycles() == batch.table_cycles()


def test_add_node_with_pii_and_new_schema(g: LineageGraph, kit: Any) -> None:
    info = TableInfo(
        TableRef("crm", "contact"), TableKind.TABLE, (ColumnInfo("phone", 1, "text", True),), ()
    )
    phone = ColumnRef(info.ref, "phone")
    pii = {phone: PiiColumn(phone, PiiCategory.DIRECT_IDENTIFIER, ErasureStrategy.NULLIFY, True)}
    add_node(g, info, pii)
    assert g.catalog.schemas == ("app", "crm")
    assert g.graph.nodes["c:crm.contact.phone"]["pii_category"] is PiiCategory.DIRECT_IDENTIFIER
    assert g.graph.nodes["t:crm.contact"]["is_artifact"] is False
    assert get_descendants(g, info.ref) == []


def test_add_node_artifact_flag_and_view_kind(g: LineageGraph, kit: Any) -> None:
    add_node(g, _info(kit, "profile_cache"), is_artifact=True)
    add_node(g, _info(kit, "v_orders", TableKind.VIEW))
    n = g.graph.nodes
    assert n["t:app.profile_cache"]["is_artifact"] is True
    assert n["c:app.profile_cache.customer_id"]["is_artifact"] is True
    assert n["t:app.v_orders"]["is_artifact"] is True


def test_add_node_errors(g: LineageGraph, kit: Any) -> None:
    with pytest.raises(GraphIntegrityError) as ei:
        add_node(g, g.catalog.tables[kit.t("customer")])
    assert ei.value.code == "DUPLICATE_TABLE"
    info = _info(kit, "fresh")
    wrong_table = ColumnRef(kit.t("customer"), "email")
    wrong_col = ColumnRef(info.ref, "missing")
    for ref in (wrong_table, wrong_col):
        pii = {ref: PiiColumn(ref, PiiCategory.SENSITIVE, ErasureStrategy.NULLIFY, True)}
        with pytest.raises(GraphIntegrityError) as ei:
            add_node(g, info, pii)
        assert ei.value.code == "GRAPH_INTEGRITY"
    # failed adds leave the graph untouched
    assert not g.catalog.has_table(info.ref)
    assert "t:app.fresh" not in g.graph


def test_add_edge_validates_and_updates_queries(g: LineageGraph, kit: Any) -> None:
    add_node(g, _info(kit, "ticket"))
    tbl, col = kit.fk_pair("fk_ticket_customer", "customer", "ticket", ("id",), ("customer_id",))
    add_edge(g, tbl)
    add_edge(g, col)
    assert kit.t("ticket") in [e.target for e in discover_dependents(g, kit.t("customer"))]
    assert kit.t("ticket") in get_descendants(g, kit.t("customer"))
    assert g.column_edges_for(tbl) == [col]
    assert g.edges() == sorted(g.edges(), key=lambda e: str(e.edge_id))
    ids = [e["id"] for e in serialize_graph(g, LineageLevel.TABLE)["edges"]]
    assert ids == sorted(ids) and str(tbl.edge_id) in ids
    with pytest.raises(GraphIntegrityError) as ei:
        add_edge(g, tbl)
    assert ei.value.code == "DUPLICATE_EDGE"
    bad = kit.edge("bad", LineageLevel.TABLE, "customer", "ghost", (), ())
    with pytest.raises(GraphIntegrityError):
        add_edge(g, bad)
    bad_col = kit.edge("bad_col", LineageLevel.COLUMN, "customer", "ticket", ("id",), ("nope",))
    with pytest.raises(GraphIntegrityError):
        add_edge(g, bad_col)
    assert bad.edge_id not in {e.edge_id for e in g.edges()}


def test_add_edge_with_artifact_id_flags_target(g: LineageGraph, kit: Any) -> None:
    assert g.graph.nodes["t:app.product"]["is_artifact"] is False
    add_edge(
        g,
        kit.edge(
            "emb",
            LineageLevel.TABLE,
            "customer",
            "product",
            ("id",),
            ("id",),
            origin=EdgeOrigin.REGISTERED_MAPPING,
            artifact_id=kit.uid("artifact:emb"),
        ),
    )
    assert g.graph.nodes["t:app.product"]["is_artifact"] is True
    assert g.graph.nodes["c:app.product.name"]["is_artifact"] is True


# ---------------------------------------------------------------------- artifacts (CR item 5)


def test_build_with_registered_artifacts_without_edges(shop: Any, kit: Any) -> None:
    plain = build_graph(shop.catalog, shop.edges)
    assert plain.graph.nodes["t:app.newsletter"]["is_artifact"] is False
    lg = build_graph(shop.catalog, shop.edges, artifacts=[kit.t("newsletter"), kit.t("newsletter")])
    assert lg.graph.nodes["t:app.newsletter"]["is_artifact"] is True
    assert lg.graph.nodes["c:app.newsletter.subscriber_ref"]["is_artifact"] is True
    node = next(
        n for n in serialize_graph(lg, LineageLevel.TABLE)["nodes"] if n["id"] == "t:app.newsletter"
    )
    assert node["is_artifact"] is True
    # unchanged rules: matviews and artifact_id targets are still flagged; others are not
    assert lg.graph.nodes["t:app.customer_ltv"]["is_artifact"] is True
    assert lg.graph.nodes["t:app.purchase_order"]["is_artifact"] is False
    # build_lineage_graph accepts the same argument (generator is fine)
    lg2 = build_lineage_graph(shop.catalog, shop.edges, None, (t for t in [kit.t("newsletter")]))
    assert serialize_graph(lg2, LineageLevel.COLUMN) == serialize_graph(lg, LineageLevel.COLUMN)


def test_unknown_artifact_table_raises(shop: Any, kit: Any) -> None:
    with pytest.raises(GraphIntegrityError) as ei:
        build_graph(shop.catalog, shop.edges, artifacts=[kit.t("ghost")])
    assert ei.value.details["table"] == "app.ghost"


def test_mark_artifact(g: LineageGraph, kit: Any) -> None:
    g.mark_artifact(kit.t("order_line"))
    g.mark_artifact(kit.t("order_line"))  # idempotent
    assert g.graph.nodes["t:app.order_line"]["is_artifact"] is True
    assert g.graph.nodes["c:app.order_line.line_no"]["is_artifact"] is True
    with pytest.raises(UnknownTableError):
        g.mark_artifact(kit.t("ghost"))


# ---------------------------------------------------------------------- evidence


def test_every_serialized_edge_has_evidence(g: LineageGraph) -> None:
    for level in (LineageLevel.TABLE, LineageLevel.COLUMN):
        out = serialize_graph(g, level)
        json.dumps(out)
        assert out["edges"]
        for e in out["edges"]:
            ev = e["evidence"]
            assert ev["origin"] == e["origin"]
            assert {"origin", "constraint_name", "artifact_id"} <= set(ev)
            assert list(ev) == sorted(ev)


def test_evidence_artifact_and_constraint(g: LineageGraph, shop: Any, kit: Any) -> None:
    out = serialize_graph(g, LineageLevel.TABLE)
    ltv = next(e for e in out["edges"] if e["id"] == str(shop.by_name("ltv_from_customer").edge_id))
    assert ltv["evidence"] == {
        "artifact_id": str(kit.ltv_art),
        "constraint_name": None,
        "origin": "SQL_LINEAGE",
    }
    fk = next(e for e in out["edges"] if e["id"] == str(shop.by_name("fk_po_customer").edge_id))
    assert fk["evidence"]["constraint_name"] == "fk_po_customer"


class _Opaque:
    def __str__(self) -> str:
        return "opaque-object"


def test_evidence_takes_details_json_safe(shop: Any, kit: Any) -> None:
    base = kit.edge(
        "sql_edge",
        LineageLevel.TABLE,
        "customer",
        "customer_ltv",
        ("id",),
        ("customer_id",),
        origin=EdgeOrigin.SQL_LINEAGE,
        artifact_id=kit.ltv_art,
    )
    details = {
        "sql": "SELECT c.id AS customer_id FROM app.customer c",
        "parser": {"name": "sqlglot", "version": "25.1.0"},
        "z_columns": ("id", "customer_id"),
        "matched_on": {"b", "a"},
        "source_ref": kit.t("customer"),
        "column_ref": ColumnRef(kit.t("customer"), "id"),
        "status": EdgeStatus.CONFIRMED,
        "uuid": UUID(int=5),
        "weight": 3,
        "flag": True,
        "nothing": None,
        "amount": Decimal("1.50"),
        "other": _Opaque(),
        "origin": "spoofed",  # structured field wins
    }
    e = replace(base, details=details)
    lg = build_graph(shop.catalog, [e])
    out = serialize_graph(lg, LineageLevel.TABLE)
    ev = out["edges"][0]["evidence"]
    assert ev == edge_evidence(e)
    assert ev == {
        "amount": "1.50",
        "artifact_id": str(kit.ltv_art),
        "column_ref": "app.customer.id",
        "constraint_name": None,
        "flag": True,
        "matched_on": ["a", "b"],
        "nothing": None,
        "origin": "SQL_LINEAGE",
        "other": "opaque-object",
        "parser": {"name": "sqlglot", "version": "25.1.0"},
        "source_ref": "app.customer",
        "sql": "SELECT c.id AS customer_id FROM app.customer c",
        "status": "CONFIRMED",
        "uuid": str(UUID(int=5)),
        "weight": 3,
        "z_columns": ["id", "customer_id"],
    }
    assert list(ev) == sorted(ev)
    # JSON round trip is stable
    assert json.loads(json.dumps(out)) == out
    assert serialize_graph(lg, LineageLevel.TABLE) == out
