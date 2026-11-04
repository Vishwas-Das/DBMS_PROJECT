from __future__ import annotations

import dataclasses

import pytest

from lae.contracts.enums import (
    EdgeOrigin,
    EdgeStatus,
    LineageLevel,
    OnDeleteAction,
    Traceability,
    Transform,
)
from lae.discovery import build_catalog, discover_foreign_keys, fk_edge_id, foreign_key_edges
from lae.errors import CatalogError
from lae.lineage import build_lineage_graph

pytestmark = pytest.mark.unit


def _catalog(kit):
    return build_catalog(kit.demo_tables(), kit.demo_fks(), ["app"])


def test_one_table_edge_plus_one_column_edge_per_pair(kit):
    cat = _catalog(kit)
    reader = kit.FakeCatalogReader(kit.demo_tables(), kit.demo_fks())
    edges = discover_foreign_keys(reader, cat)
    fks = kit.demo_fks()
    table_edges = [e for e in edges if e.level is LineageLevel.TABLE]
    column_edges = [e for e in edges if e.level is LineageLevel.COLUMN]
    assert len(table_edges) == len(fks)
    assert len(column_edges) == sum(len(f.child_columns) for f in fks)
    for e in edges:
        assert e.origin is EdgeOrigin.DECLARED_FK
        assert e.transform is Transform.FOREIGN_KEY
        assert e.traceability is Traceability.KEY_PRESERVING
        assert e.status is EdgeStatus.CONFIRMED
        assert e.on_delete is not None and e.constraint_name
        assert e.details["evidence"] == "pg_constraint"
        assert e.details["constraint"] == e.constraint_name
        assert e.details["on_delete"] == e.on_delete.value
    for e in column_edges:
        assert len(e.source_columns) == len(e.target_columns) == 1
    # the graph engineer's builder accepts all of them
    build_lineage_graph(cat, edges)


def test_composite_fk_keeps_column_order(kit):
    cat = _catalog(kit)
    edges = foreign_key_edges(kit.demo_fks(), cat)
    ship = [e for e in edges if e.constraint_name == "shipment_order_line_fkey"]
    table_edge = next(e for e in ship if e.level is LineageLevel.TABLE)
    assert table_edge.source == kit.t("order_line") and table_edge.target == kit.t("shipment")
    assert table_edge.source_columns == ("order_id", "line_no")
    assert table_edge.target_columns == ("order_id", "line_no")
    cols = sorted(
        (e for e in ship if e.level is LineageLevel.COLUMN), key=lambda e: e.details["pair_index"]
    )
    assert [(e.source_columns, e.target_columns) for e in cols] == [
        (("order_id",), ("order_id",)),
        (("line_no",), ("line_no",)),
    ]
    assert all(e.details["pair_count"] == 2 for e in cols)


def test_composite_fk_with_swapped_column_order_is_preserved(kit):
    tables = [
        kit.table("p", [("a", "int"), ("b", "int")], ("a", "b")),
        kit.table("c", [("id", "int"), ("x", "int"), ("y", "int")], ("id",)),
    ]
    fk = kit.fk("c_fk", "c", ("y", "x"), "p", ("b", "a"))
    edges = foreign_key_edges([fk], build_catalog(tables, [fk], ["app"]))
    te = edges[0]
    assert te.source_columns == ("b", "a") and te.target_columns == ("y", "x")
    assert [(e.source_columns, e.target_columns) for e in edges[1:]] == [
        (("b",), ("y",)),
        (("a",), ("x",)),
    ]


@pytest.mark.parametrize("code", list(OnDeleteAction))
def test_every_on_delete_code_is_preserved(kit, code):
    tables = [kit.table("p", [("id", "int")], ("id",)), kit.table("c", [("pid", "int")])]
    fk = kit.fk("c_pid_fkey", "c", ("pid",), "p", ("id",), code)
    edges = foreign_key_edges([fk], build_catalog(tables, [fk], ["app"]))
    assert {e.on_delete for e in edges} == {code}
    assert edges[0].details["on_delete"] == code.value
    assert edges[0].details["on_update"] == "a"
    assert edges[0].details["deferrable"] is False


def test_edge_ids_are_deterministic_and_distinct(kit):
    cat = _catalog(kit)
    a = foreign_key_edges(kit.demo_fks(), cat)
    b = foreign_key_edges(list(reversed(kit.demo_fks())), cat)
    assert [e.edge_id for e in a] == [e.edge_id for e in b]
    assert len({e.edge_id for e in a}) == len(a)
    fk0 = kit.demo_fks()[0]
    assert fk_edge_id(fk0, LineageLevel.TABLE) in {e.edge_id for e in a}
    # the same constraint name on another table yields another id
    other = dataclasses.replace(fk0, child=kit.t("customer_address"))
    assert fk_edge_id(other, LineageLevel.TABLE) != fk_edge_id(fk0, LineageLevel.TABLE)
    assert fk_edge_id(fk0, LineageLevel.COLUMN, 0) != fk_edge_id(fk0, LineageLevel.COLUMN, 1)


def test_self_reference_edge(kit):
    edges = foreign_key_edges(kit.demo_fks(), _catalog(kit))
    ref = next(
        e
        for e in edges
        if e.constraint_name == "customer_referrer_id_fkey" and e.level is LineageLevel.TABLE
    )
    assert ref.source == ref.target == kit.t("customer")
    assert ref.source_columns == ("id",) and ref.target_columns == ("referrer_id",)
    assert ref.on_delete is OnDeleteAction.SET_NULL


def test_parent_outside_catalog_is_skipped(kit, caplog):
    tables = [kit.table("c", [("pid", "int")])]
    fk = dataclasses.replace(kit.fk("c_fk", "c", ("pid",), "p", ("id",)), parent=kit.t("p", "ext"))
    cat = build_catalog(tables, [fk], ["app"])
    assert foreign_key_edges([fk], cat) == []
    assert "outside the scanned schemas" in caplog.text


@pytest.mark.parametrize(
    "bad",
    [
        "mismatched",
        "missing_child_col",
        "missing_parent_col",
        "unknown_child",
        "duplicate",
        "empty",
    ],
)
def test_inconsistent_fk_rows_raise(kit, bad):
    tables = [kit.table("p", [("id", "int")], ("id",)), kit.table("c", [("pid", "int")])]
    cat = build_catalog(tables, [], ["app"])
    fk = kit.fk("c_fk", "c", ("pid",), "p", ("id",))
    rows = [fk]
    if bad == "mismatched":
        rows = [dataclasses.replace(fk, child_columns=("pid", "pid"))]
    elif bad == "missing_child_col":
        rows = [dataclasses.replace(fk, child_columns=("nope",))]
    elif bad == "missing_parent_col":
        rows = [dataclasses.replace(fk, parent_columns=("nope",))]
    elif bad == "unknown_child":
        rows = [dataclasses.replace(fk, child=kit.t("zzz"))]
    elif bad == "duplicate":
        rows = [fk, fk]
    else:
        rows = [dataclasses.replace(fk, child_columns=(), parent_columns=())]
    with pytest.raises(CatalogError) as e:
        foreign_key_edges(rows, cat)
    assert "constraint" in e.value.details
