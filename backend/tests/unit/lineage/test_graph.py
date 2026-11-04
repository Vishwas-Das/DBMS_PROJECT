from __future__ import annotations

import json
from typing import Any

import pytest

from lae.contracts.enums import (
    EdgeOrigin,
    EdgeStatus,
    ErasureStrategy,
    LineageLevel,
    PiiCategory,
    TableKind,
)
from lae.contracts.types import CatalogSnapshot, ColumnInfo, ColumnRef, PiiColumn, TableInfo
from lae.errors import GraphIntegrityError, UnknownTableError
from lae.lineage import (
    CONFIRMED_ONLY,
    LineageGraph,
    build_lineage_graph,
    column_node_id,
    table_node_id,
)

pytestmark = pytest.mark.unit

ALL = frozenset(EdgeStatus)


@pytest.fixture
def g(shop: Any, kit: Any) -> LineageGraph:
    email = ColumnRef(kit.t("customer"), "email")
    pii = {
        email: PiiColumn(
            column=email,
            category=PiiCategory.DIRECT_IDENTIFIER,
            strategy=ErasureStrategy.NULLIFY,
            is_confirmed=True,
        )
    }
    return build_lineage_graph(shop.catalog, shop.edges, pii)


# ---------------------------------------------------------------------- construction


def test_nodes_and_attributes(g: LineageGraph, kit: Any) -> None:
    n = g.graph.nodes
    assert n["t:app.customer"]["kind"] == "table"
    assert n["t:app.customer"]["pk"] == ("id",)
    assert n["t:app.order_line"]["pk"] == ("order_id", "line_no")
    assert n["t:app.customer_ltv"]["is_artifact"] is True
    assert n["t:app.purchase_order"]["is_artifact"] is False
    assert n["c:app.customer.email"]["pii_category"] is PiiCategory.DIRECT_IDENTIFIER
    assert n["c:app.customer.id"]["pii_category"] is None
    assert n["c:app.order_line.line_no"]["data_type"] == "integer"
    assert table_node_id(kit.t("customer")) == "t:app.customer"
    assert column_node_id(ColumnRef(kit.t("customer"), "id")) == "c:app.customer.id"
    assert g.edge(kit.uid("fk_po_customer")).constraint_name == "fk_po_customer"


def test_edges_carry_relationship_edge(g: LineageGraph, shop: Any) -> None:
    e = shop.by_name("fk_po_customer")
    data = g.graph.get_edge_data("t:app.customer", "t:app.purchase_order")
    assert data[str(e.edge_id)]["edge"] is e
    col = shop.by_name("fk_po_customer:0")
    cdata = g.graph.get_edge_data("c:app.customer.id", "c:app.purchase_order.customer_id")
    assert cdata[f"{col.edge_id}:0"]["edge"] is col


def test_default_pii_is_none(shop: Any) -> None:
    g = build_lineage_graph(shop.catalog, shop.edges)
    assert g.graph.nodes["c:app.customer.email"]["pii_category"] is None


def test_unknown_table_edge_raises(shop: Any, kit: Any) -> None:
    bad = kit.edge("bad", LineageLevel.TABLE, "customer", "ghost", (), ())
    with pytest.raises(GraphIntegrityError) as ei:
        build_lineage_graph(shop.catalog, [*shop.edges, bad])
    assert ei.value.details["table"] == "app.ghost"
    bad_src = kit.edge("bad2", LineageLevel.TABLE, "ghost", "customer", (), ())
    with pytest.raises(GraphIntegrityError):
        build_lineage_graph(shop.catalog, [bad_src])


def test_unknown_column_edge_raises(shop: Any, kit: Any) -> None:
    bad = kit.edge("bad", LineageLevel.COLUMN, "customer", "purchase_order", ("id",), ("nope",))
    with pytest.raises(GraphIntegrityError) as ei:
        build_lineage_graph(shop.catalog, [bad])
    assert ei.value.details["column"] == "nope"
    bad_src = kit.edge("bad", LineageLevel.TABLE, "customer", "purchase_order", ("x",), ("id",))
    with pytest.raises(GraphIntegrityError):
        build_lineage_graph(shop.catalog, [bad_src])


def test_duplicate_edge_id_raises(shop: Any) -> None:
    with pytest.raises(GraphIntegrityError) as ei:
        build_lineage_graph(shop.catalog, [*shop.edges, shop.edges[0]])
    assert ei.value.code == "DUPLICATE_EDGE"


def test_unknown_pii_column_raises(shop: Any, kit: Any) -> None:
    for ref in (ColumnRef(kit.t("customer"), "phone"), ColumnRef(kit.t("ghost"), "x")):
        pii = {ref: PiiColumn(ref, PiiCategory.QUASI_IDENTIFIER, ErasureStrategy.NULLIFY, False)}
        with pytest.raises(GraphIntegrityError):
            build_lineage_graph(shop.catalog, shop.edges, pii)


def test_unknown_edge_lookup(g: LineageGraph, kit: Any) -> None:
    with pytest.raises(GraphIntegrityError):
        g.edge(kit.uid("nope"))


def test_edges_listing_sorted(g: LineageGraph) -> None:
    all_edges = g.edges()
    assert [str(e.edge_id) for e in all_edges] == sorted(str(e.edge_id) for e in all_edges)
    assert all(e.status is EdgeStatus.CONFIRMED for e in g.edges(CONFIRMED_ONLY))
    assert len(g.edges(CONFIRMED_ONLY)) < len(all_edges)


# ---------------------------------------------------------------------- views


def test_table_view_excludes_suggested_by_default(g: LineageGraph) -> None:
    v = g.table_view()
    assert set(v.nodes) >= {"t:app.customer", "t:app.newsletter"}
    assert not v.has_edge("t:app.customer", "t:app.newsletter")
    assert v.has_edge("t:app.customer", "t:app.purchase_order")
    assert v.has_edge("t:app.customer", "t:app.customer")
    assert all(d["edge"].level is LineageLevel.TABLE for _, _, d in v.edges(data=True))
    assert g.table_view(ALL).has_edge("t:app.customer", "t:app.newsletter")
    # views are copies
    v.add_edge("t:app.product", "t:app.customer")
    assert not g.table_view().has_edge("t:app.product", "t:app.customer")


def test_column_view(g: LineageGraph) -> None:
    v = g.column_view()
    assert v.has_edge("t:app.customer", "c:app.customer.email")
    assert v.has_edge("c:app.customer.id", "c:app.purchase_order.customer_id")
    assert not v.has_edge("c:app.customer.id", "c:app.newsletter.subscriber_ref")
    assert g.column_view(ALL).has_edge("c:app.customer.id", "c:app.newsletter.subscriber_ref")
    lineage = [d for _, _, d in v.edges(data=True) if "edge" in d]
    assert lineage and all(d["edge"].level is LineageLevel.COLUMN for d in lineage)


# ---------------------------------------------------------------------- traversal


def test_downstream_filters_and_orders(g: LineageGraph, kit: Any) -> None:
    down = g.downstream(kit.t("customer"))
    assert [e.target.name for e in down] == ["customer", "customer_ltv", "purchase_order"]
    assert all(e.level is LineageLevel.TABLE for e in down)
    assert all(e.status is EdgeStatus.CONFIRMED for e in down)
    down_all = g.downstream(kit.t("customer"), ALL)
    assert [e.target.name for e in down_all] == [
        "customer",
        "customer_ltv",
        "newsletter",
        "purchase_order",
    ]
    assert g.downstream(kit.t("customer_segment")) == []


def test_downstream_parallel_edges_tie_break(shop: Any, kit: Any) -> None:
    # two FKs between the same tables: ordering falls back to columns, then edge_id
    a = kit.edge("z_fk", LineageLevel.TABLE, "customer", "newsletter", ("id",), ("subscriber_ref",))
    b = kit.edge("a_fk", LineageLevel.TABLE, "customer", "newsletter", ("id",), ("id",))
    c = kit.edge("m_fk", LineageLevel.TABLE, "customer", "newsletter", ("id",), ("id",))
    g = build_lineage_graph(shop.catalog, [a, b, c])
    down = g.downstream(kit.t("customer"))
    assert down[-1] is a
    assert [str(e.edge_id) for e in down[:2]] == sorted([str(b.edge_id), str(c.edge_id)])


def test_upstream(g: LineageGraph, kit: Any) -> None:
    up = g.upstream(kit.t("order_line"))
    assert [e.source.name for e in up] == ["product", "purchase_order"]
    up_ltv = g.upstream(kit.t("customer_ltv"))
    assert [e.source.name for e in up_ltv] == ["customer", "purchase_order"]
    assert g.upstream(kit.t("newsletter")) == []
    assert len(g.upstream(kit.t("newsletter"), ALL)) == 1


def test_traversal_unknown_table(g: LineageGraph, kit: Any) -> None:
    for fn in (g.downstream, g.upstream, g.reachable_from):
        with pytest.raises(UnknownTableError):
            fn(kit.t("ghost"))
    with pytest.raises(UnknownTableError):
        g.artifact_order([kit.t("ghost")])


def test_column_edges_for(g: LineageGraph, shop: Any) -> None:
    tbl = shop.by_name("fk_po_customer")
    cols = g.column_edges_for(tbl)
    assert [e.edge_id for e in cols] == [shop.by_name("fk_po_customer:0").edge_id]
    ltv = g.column_edges_for(shop.by_name("ltv_from_customer"))
    assert [e.edge_id for e in ltv] == [shop.by_name("ltv_from_customer:col").edge_id]
    assert g.column_edges_for(shop.by_name("ltv_from_order")) == []
    with pytest.raises(GraphIntegrityError):
        g.column_edges_for(cols[0])


def test_column_edges_for_composite(shop: Any, kit: Any) -> None:
    edges = kit.fk_pair(
        "fk_comp", "order_line", "newsletter", ("order_id", "line_no"), ("id", "subscriber_ref")
    )
    g = build_lineage_graph(shop.catalog, edges)
    cols = g.column_edges_for(edges[0])
    assert [e.source_columns for e in cols] == [("line_no",), ("order_id",)]


def test_reachable_from(g: LineageGraph, kit: Any) -> None:
    names = {t.name for t in g.reachable_from(kit.t("customer"))}
    assert names == {"purchase_order", "order_line", "customer_ltv", "customer_segment"}
    assert "newsletter" in {t.name for t in g.reachable_from(kit.t("customer"), ALL)}
    assert g.reachable_from(kit.t("customer_segment")) == set()


def test_table_cycles(g: LineageGraph, shop: Any, kit: Any) -> None:
    assert g.table_cycles() == [[kit.t("customer")]]
    extra = [
        kit.edge("c1", LineageLevel.TABLE, "purchase_order", "product", (), ()),
        kit.edge("c2", LineageLevel.TABLE, "product", "purchase_order", (), ()),
        kit.edge("c2b", LineageLevel.TABLE, "product", "purchase_order", (), ()),
        kit.edge(
            "c3", LineageLevel.TABLE, "order_line", "customer", (), (), status=EdgeStatus.SUGGESTED
        ),
    ]
    g2 = build_lineage_graph(shop.catalog, [*shop.edges, *extra])
    cycles = g2.table_cycles()
    assert cycles == sorted(cycles)
    assert [kit.t("customer")] in cycles
    assert [kit.t("product"), kit.t("purchase_order")] in cycles
    # parallel edges do not duplicate cycles
    assert len(cycles) == 2
    # the SUGGESTED edge only counts when asked for
    assert len(g2.table_cycles(ALL)) > len(cycles)
    for c in g2.table_cycles(ALL):
        assert c[0] == min(c)


def test_artifact_order(g: LineageGraph, kit: Any) -> None:
    seg, ltv = kit.t("customer_segment"), kit.t("customer_ltv")
    assert g.artifact_order([seg, ltv]) == [ltv, seg]
    assert g.artifact_order([seg, ltv, seg]) == [ltv, seg]
    assert g.artifact_order([]) == []
    # independent artifacts fall back to TableRef order
    assert g.artifact_order([kit.t("product"), kit.t("newsletter")]) == [
        kit.t("newsletter"),
        kit.t("product"),
    ]


def test_artifact_order_cycle_raises(shop: Any, kit: Any) -> None:
    back = kit.edge("back", LineageLevel.TABLE, "customer_segment", "customer_ltv", (), ())
    g = build_lineage_graph(shop.catalog, [*shop.edges, back])
    with pytest.raises(GraphIntegrityError) as ei:
        g.artifact_order([kit.t("customer_ltv"), kit.t("customer_segment")])
    assert ei.value.code == "ARTIFACT_CYCLE"
    selfloop = kit.edge("self", LineageLevel.TABLE, "customer_ltv", "customer_ltv", (), ())
    g2 = build_lineage_graph(shop.catalog, [selfloop])
    with pytest.raises(GraphIntegrityError):
        g2.artifact_order([kit.t("customer_ltv")])


def test_artifact_order_ignores_cycles_elsewhere(g: LineageGraph, kit: Any) -> None:
    # customer has a self-FK; it is not an artifact so ordering still works
    assert g.artifact_order([kit.t("customer_segment")]) == [kit.t("customer_segment")]


# ---------------------------------------------------------------------- node-link


def test_to_node_link_table(g: LineageGraph, shop: Any) -> None:
    out = g.to_node_link(LineageLevel.TABLE)
    json.dumps(out)  # plain JSON-able
    assert set(out) == {"nodes", "edges"}
    ids = [n["id"] for n in out["nodes"]]
    assert ids == sorted(ids) and all(i.startswith("t:") for i in ids)
    assert len(ids) == len(shop.catalog.tables)
    node = next(n for n in out["nodes"] if n["id"] == "t:app.customer_ltv")
    assert node == {
        "id": "t:app.customer_ltv",
        "kind": "table",
        "schema_name": "app",
        "table_name": "customer_ltv",
        "column_name": None,
        "table_kind": "MATERIALIZED_VIEW",
        "pii_category": None,
        "is_artifact": True,
    }
    fk = shop.by_name("fk_ol_order")
    e = next(x for x in out["edges"] if x["id"] == str(fk.edge_id))
    assert e == {
        "id": str(fk.edge_id),
        "source": "t:app.purchase_order",
        "target": "t:app.order_line",
        "level": "TABLE",
        "origin": "DECLARED_FK",
        "transform": "FOREIGN_KEY",
        "traceability": "KEY_PRESERVING",
        "status": "CONFIRMED",
        "source_columns": ["id"],
        "target_columns": ["order_id"],
        "on_delete_action": "c",
        "confidence": 1.0,
    }
    statuses = {x["status"] for x in out["edges"]}
    assert "SUGGESTED" in statuses  # UI needs suggested edges to review them
    confirmed = g.to_node_link(LineageLevel.TABLE, CONFIRMED_ONLY)
    assert {x["status"] for x in confirmed["edges"]} == {"CONFIRMED"}
    assert all(x["level"] == "TABLE" for x in out["edges"])
    # deterministic
    assert g.to_node_link(LineageLevel.TABLE) == out


def test_to_node_link_column(g: LineageGraph, shop: Any, kit: Any) -> None:
    out = g.to_node_link(LineageLevel.COLUMN)
    json.dumps(out)
    kinds = {n["kind"] for n in out["nodes"]}
    assert kinds == {"table", "column"}
    email = next(n for n in out["nodes"] if n["id"] == "c:app.customer.email")
    assert email["pii_category"] == "DIRECT_IDENTIFIER"
    assert email["column_name"] == "email"
    assert email["table_kind"] is None
    assert all(x["level"] == "COLUMN" for x in out["edges"])
    e = next(
        x for x in out["edges"] if x["id"] == str(shop.by_name("ltv_from_customer:col").edge_id)
    )
    assert e["source"] == "c:app.customer.id"
    assert e["target"] == "c:app.customer_ltv.customer_id"
    assert e["on_delete_action"] is None
    with pytest.raises(GraphIntegrityError):
        g.to_node_link(LineageLevel.RECORD)


def test_view_kind_is_artifact(kit: Any) -> None:
    v = TableInfo(kit.t("v"), TableKind.VIEW, (ColumnInfo("a", 1, "int", True),), ())
    cat = CatalogSnapshot({v.ref: v}, ("app",), "0" * 64)
    g = build_lineage_graph(cat, [])
    assert g.graph.nodes["t:app.v"]["is_artifact"] is True
    assert g.to_node_link(LineageLevel.TABLE)["edges"] == []
    assert g.table_cycles() == []


def test_registered_mapping_origin_roundtrip(shop: Any, kit: Any) -> None:
    e = kit.edge(
        "reg",
        LineageLevel.TABLE,
        "customer",
        "product",
        ("id",),
        ("id",),
        origin=EdgeOrigin.REGISTERED_MAPPING,
        artifact_id=kit.uid("art"),
    )
    g = build_lineage_graph(shop.catalog, [e])
    assert g.graph.nodes["t:app.product"]["is_artifact"] is True
    assert g.to_node_link(LineageLevel.TABLE)["edges"][0]["origin"] == "REGISTERED_MAPPING"
