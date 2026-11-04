from __future__ import annotations

import hashlib
import json
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid5

import pytest

from lae.audit.canonical import canonical_hash
from lae.contracts.enums import RecordRole, Traceability
from lae.contracts.types import RecordKey, TableRef
from lae.errors import GraphIntegrityError
from lae.lineage import DeletionOrder, RecordAttrs, RecordDiscoveryResult, RecordGraph

pytestmark = pytest.mark.unit

NS = UUID("00000000-0000-0000-0000-0000000000bb")
OWNED = RecordRole.OWNED

FK_PO = uuid5(NS, "fk_po_customer")
FK_OL = uuid5(NS, "fk_ol_order")
FK_REF = uuid5(NS, "fk_customer_referrer")
FK_BILL = uuid5(NS, "fk_po_billing")


def cust(i: int) -> RecordKey:
    return RecordKey(TableRef("app", "customer"), ("id",), (i,))


def po(i: int) -> RecordKey:
    return RecordKey(TableRef("app", "purchase_order"), ("id",), (i,))


def ol(o: int, n: int) -> RecordKey:
    return RecordKey(TableRef("app", "order_line"), ("order_id", "line_no"), (o, n))


def shop_records() -> RecordGraph:
    """customer(42) -> po(1001, 1002) -> order_line(1001,1),(1001,2),(1002,1)."""
    rg = RecordGraph()
    rg.add_root(cust(42))
    for o in (1001, 1002):
        rg.add_dependency(cust(42), po(o), FK_PO, OWNED)
    for o, n in ((1001, 1), (1001, 2), (1002, 1)):
        rg.add_dependency(po(o), ol(o, n), FK_OL, OWNED)
    rg.add_dependency(cust(42), cust(77), FK_REF, RecordRole.REFERENCING_OTHER_SUBJECT)
    return rg


def position(order: tuple[RecordKey, ...]) -> dict[RecordKey, int]:
    return {k: i for i, k in enumerate(order)}


# ---------------------------------------------------------------------- building


def test_add_root_and_dependency() -> None:
    rg = RecordGraph()
    rg.add_root(cust(42))
    assert rg.attrs(cust(42)) == RecordAttrs(RecordRole.SUBJECT_ROOT, 0, ())
    assert rg.add_dependency(cust(42), po(1), FK_PO, OWNED) is True
    assert rg.add_dependency(po(1), ol(1, 1), FK_OL, OWNED) is True
    assert rg.attrs(ol(1, 1)).depth == 2
    assert len(rg) == 3
    assert po(1) in rg and po(2) not in rg


def test_add_dependency_existing_child_accumulates() -> None:
    rg = RecordGraph()
    rg.add_root(cust(42))
    rg.add_dependency(cust(42), po(1), FK_PO, OWNED)
    rg.add_dependency(po(1), ol(1, 1), FK_OL, OWNED)
    # a deeper path to po(1) does not increase its depth
    rg.add_dependency(ol(1, 1), po(1), FK_BILL, OWNED)
    assert rg.add_dependency(cust(42), po(1), FK_BILL, OWNED) is False
    a = rg.attrs(po(1))
    assert a.depth == 1
    assert a.via_edge_ids == tuple(sorted({FK_PO, FK_BILL}, key=str))
    # two lineage edges between the same rows are both kept
    assert {(p, c, e) for p, c, e in rg.edges() if p == cust(42)} == {
        (cust(42), po(1), FK_PO),
        (cust(42), po(1), FK_BILL),
    }


def test_depth_takes_minimum() -> None:
    rg = RecordGraph()
    rg.add_root(cust(42))
    rg.add_dependency(cust(42), po(1), FK_PO, OWNED)
    rg.add_dependency(po(1), ol(1, 1), FK_OL, OWNED)
    assert rg.attrs(ol(1, 1)).depth == 2
    rg.add_dependency(cust(42), ol(1, 1), FK_BILL, OWNED)
    assert rg.attrs(ol(1, 1)).depth == 1


def test_role_precedence_is_protective() -> None:
    rg = RecordGraph()
    rg.add_root(cust(42))
    rg.add_dependency(cust(42), po(1), FK_PO, OWNED)
    rg.add_dependency(cust(42), po(1), FK_BILL, RecordRole.SHARED)
    assert rg.attrs(po(1)).role is RecordRole.SHARED
    rg.add_dependency(cust(42), po(1), FK_REF, OWNED)
    assert rg.attrs(po(1)).role is RecordRole.SHARED


def test_add_root_upgrades_existing() -> None:
    rg = RecordGraph()
    rg.add_root(cust(42))
    rg.add_dependency(cust(42), cust(77), FK_REF, RecordRole.REFERENCING_OTHER_SUBJECT)
    rg.add_root(cust(77))
    assert rg.attrs(cust(77)) == RecordAttrs(RecordRole.SUBJECT_ROOT, 0, (FK_REF,))


def test_add_dependency_errors() -> None:
    rg = RecordGraph()
    with pytest.raises(GraphIntegrityError) as ei:
        rg.add_dependency(cust(42), po(1), FK_PO, OWNED)
    assert ei.value.code == "UNKNOWN_PARENT"
    assert "42" not in json.dumps(ei.value.details)
    rg.add_root(cust(42))
    with pytest.raises(GraphIntegrityError):
        rg.add_dependency(cust(42), po(1), FK_PO, RecordRole.SUBJECT_ROOT)
    with pytest.raises(GraphIntegrityError):
        rg.attrs(po(999))


def test_nodes_edges_by_table_sorted() -> None:
    rg = shop_records()
    keys = [k for k, _ in rg.nodes()]
    assert keys == sorted(keys, key=RecordKey.sort_key)
    assert keys[0] == cust(42)
    edges = rg.edges()
    assert edges == sorted(edges, key=lambda e: (e[0].sort_key(), e[1].sort_key(), str(e[2])))
    bt = rg.by_table()
    assert list(bt) == sorted(bt)
    assert bt[TableRef("app", "order_line")] == [ol(1001, 1), ol(1001, 2), ol(1002, 1)]
    assert bt[TableRef("app", "customer")] == [cust(42), cust(77)]


def test_mixed_int_str_keys_sort() -> None:
    rg = RecordGraph()
    t = TableRef("app", "x")
    rg.add_root(RecordKey(t, ("id",), ("b",)))
    rg.add_dependency(RecordKey(t, ("id",), ("b",)), RecordKey(t, ("id",), (10,)), FK_PO, OWNED)
    rg.add_dependency(RecordKey(t, ("id",), ("b",)), RecordKey(t, ("id",), (9,)), FK_PO, OWNED)
    assert [k.values for k in rg.by_table()[t]] == [(9,), (10,), ("b",)]


# ---------------------------------------------------------------------- deletion order


def test_deletion_order_dag_children_first() -> None:
    rg = shop_records()
    d = rg.deletion_order(lambda _e: False)
    assert isinstance(d, DeletionOrder)
    assert set(d.order) == {k for k, _ in rg.nodes()}
    assert d.break_edges == ()
    pos = position(d.order)
    for parent, child, _ in rg.edges():
        assert pos[child] < pos[parent]
    # deterministic
    assert rg.deletion_order(lambda _e: False) == d


def test_deletion_order_ignores_self_reference() -> None:
    rg = RecordGraph()
    rg.add_root(cust(42))
    rg.add_dependency(cust(42), cust(42), FK_REF, OWNED)
    d = rg.deletion_order(lambda _e: False)
    assert d.order == (cust(42),)
    assert d.break_edges == ()


def test_scc_broken_via_nullable_edge() -> None:
    # po(1) -> ol(1,1) (not nullable) and ol(1,1) -> po(1) via a nullable back reference
    rg = RecordGraph()
    rg.add_root(cust(42))
    rg.add_dependency(cust(42), po(1), FK_PO, OWNED)
    rg.add_dependency(po(1), ol(1, 1), FK_OL, OWNED)
    rg.add_dependency(ol(1, 1), po(1), FK_REF, OWNED)
    d = rg.deletion_order(lambda e: e == FK_REF)
    assert d.break_edges == ((ol(1, 1), po(1), FK_REF),)
    pos = position(d.order)
    assert pos[ol(1, 1)] < pos[po(1)] < pos[cust(42)]


def test_scc_breaks_only_what_is_needed() -> None:
    # 3-cycle a->b->c->a where every edge is nullable: one break suffices
    rg = RecordGraph()
    rg.add_root(cust(1))
    rg.add_dependency(cust(1), cust(2), FK_REF, OWNED)
    rg.add_dependency(cust(2), cust(3), FK_REF, OWNED)
    rg.add_dependency(cust(3), cust(1), FK_REF, OWNED)
    d = rg.deletion_order(lambda _e: True)
    assert len(d.break_edges) == 1
    broken = {(p, c) for p, c, _ in d.break_edges}
    pos = position(d.order)
    for parent, child, _ in rg.edges():
        if (parent, child) not in broken:
            assert pos[child] < pos[parent]
    assert rg.deletion_order(lambda _e: True) == d


def test_multi_edge_pair_needs_all_ids_nullable() -> None:
    rg = RecordGraph()
    rg.add_root(cust(1))
    rg.add_dependency(cust(1), cust(2), FK_PO, OWNED)
    rg.add_dependency(cust(2), cust(1), FK_REF, OWNED)
    rg.add_dependency(cust(2), cust(1), FK_BILL, OWNED)
    with pytest.raises(GraphIntegrityError):
        rg.deletion_order(lambda e: e == FK_REF)
    d = rg.deletion_order(lambda e: e in (FK_REF, FK_BILL))
    assert d.break_edges == tuple(
        sorted([(cust(2), cust(1), FK_REF), (cust(2), cust(1), FK_BILL)], key=lambda x: str(x[2]))
    )


def test_unbreakable_cycle_raises_without_values() -> None:
    rg = RecordGraph()
    rg.add_root(cust(424242))
    rg.add_dependency(cust(424242), po(555555), FK_PO, OWNED)
    rg.add_dependency(po(555555), cust(424242), FK_BILL, OWNED)
    with pytest.raises(GraphIntegrityError) as ei:
        rg.deletion_order(lambda _e: False)
    err = ei.value
    assert err.code == "UNBREAKABLE_CYCLE"
    assert err.details == {"tables": ["app.customer", "app.purchase_order"], "record_count": 2}
    assert "424242" not in str(err) and "555555" not in json.dumps(err.details)


# ---------------------------------------------------------------------- hashing


def test_record_set_hash_stable_across_insertion_order() -> None:
    a = shop_records()
    b = RecordGraph()
    b.add_root(cust(42))
    b.add_dependency(cust(42), cust(77), FK_REF, RecordRole.REFERENCING_OTHER_SUBJECT)
    b.add_dependency(cust(42), po(1002), FK_PO, OWNED)
    b.add_dependency(po(1002), ol(1002, 1), FK_OL, OWNED)
    b.add_dependency(cust(42), po(1001), FK_PO, OWNED)
    b.add_dependency(po(1001), ol(1001, 2), FK_OL, OWNED)
    b.add_dependency(po(1001), ol(1001, 1), FK_OL, OWNED)
    assert a.record_set_hash() == b.record_set_hash()
    assert len(a.record_set_hash()) == 64
    b.add_dependency(po(1001), ol(1001, 3), FK_OL, OWNED)
    assert a.record_set_hash() != b.record_set_hash()


def test_record_set_hash_known_value() -> None:
    rg = RecordGraph()
    rg.add_root(cust(42))
    expected_json = '[{"columns":["id"],"is_predicate":false,"table":"app.customer","values":[42]}]'
    assert rg.record_set_hash() == hashlib.sha256(expected_json.encode()).hexdigest()
    assert rg.record_set_hash() == canonical_hash([cust(42).as_canonical()])
    assert RecordGraph().record_set_hash() == hashlib.sha256(b"[]").hexdigest()


def test_record_set_hash_ignores_edges_and_roles() -> None:
    a = RecordGraph()
    a.add_root(cust(42))
    a.add_dependency(cust(42), po(1), FK_PO, OWNED)
    b = RecordGraph()
    b.add_root(cust(42))
    b.add_dependency(cust(42), po(1), FK_BILL, RecordRole.SHARED)
    assert a.record_set_hash() == b.record_set_hash()


# ---------------------------------------------------------------------- node-link


def test_to_node_link_has_no_raw_values() -> None:
    rg = RecordGraph()
    secret_root = RecordKey(TableRef("app", "customer"), ("email",), ("alice@example.com",))
    rg.add_root(secret_root)
    rg.add_dependency(secret_root, po(987654321), FK_PO, OWNED)
    rg.add_dependency(po(987654321), ol(987654321, 31337), FK_OL, OWNED)

    def fake_hmac(k: RecordKey) -> str:
        return hashlib.sha256(b"k" + json.dumps(k.as_canonical()).encode()).hexdigest()

    out = rg.to_node_link(fake_hmac)
    blob = json.dumps(out)
    for raw in ("alice@example.com", "987654321", "31337"):
        assert raw not in blob
    assert {n["id"] for n in out["nodes"]} == {fake_hmac(k) for k, _ in rg.nodes()}
    assert len(out["edges"]) == 2
    node = next(n for n in out["nodes"] if n["table"] == "app.order_line")
    assert node["key_columns"] == ["order_id", "line_no"]
    assert node["role"] == "OWNED" and node["depth"] == 2
    assert node["via_edge_ids"] == [str(FK_OL)]
    assert out["nodes"] == sorted(out["nodes"], key=lambda n: (n["table"], n["id"]))
    assert rg.to_node_link(fake_hmac) == out


# ---------------------------------------------------------------------- results


def test_record_discovery_result_freezes_and_sorts() -> None:
    rg = shop_records()
    res = RecordDiscoveryResult(
        record_graph=rg,
        affected_artifacts={
            TableRef("app", "z_mv"): Traceability.OPAQUE,
            TableRef("app", "customer_ltv"): Traceability.AGGREGATE,
        },
        warnings=("w",),
    )
    assert isinstance(res.affected_artifacts, MappingProxyType)
    assert list(res.affected_artifacts) == [
        TableRef("app", "customer_ltv"),
        TableRef("app", "z_mv"),
    ]
    assert res.record_graph is rg
    assert res.warnings == ("w",)
    with pytest.raises(AttributeError):
        res.warnings = ()  # type: ignore[misc]
    empty: Any = RecordDiscoveryResult(RecordGraph(), {})
    assert empty.warnings == ()
