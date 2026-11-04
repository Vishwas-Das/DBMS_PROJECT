"""Record-level discovery against the hand-built dataset in conftest.py (subject: customer 42)."""

from __future__ import annotations

from uuid import UUID

import pytest

from lae.contracts.enums import (
    EdgeOrigin,
    EdgeStatus,
    LineageLevel,
    RecordRole,
    Traceability,
    Transform,
)
from lae.contracts.types import DiscoveryLimits, RecordKey, RelationshipEdge, SubjectRoot
from lae.discovery import discover_records, record_set_fingerprint
from lae.errors import DiscoveryLimitExceeded, SubjectNotFoundError, ValidationError

pytestmark = pytest.mark.unit

OWNED, SHARED, REF, ROOT = (
    RecordRole.OWNED,
    RecordRole.SHARED,
    RecordRole.REFERENCING_OTHER_SUBJECT,
    RecordRole.SUBJECT_ROOT,
)


def _k(kit, table, *values, cols=("id",), pred=False):
    return RecordKey(kit.t(table), cols, values, is_predicate=pred)


def _expected(kit) -> dict[RecordKey, RecordRole]:
    k = lambda *a, **kw: _k(kit, *a, **kw)  # noqa: E731
    return {
        k("customer", 42): ROOT,
        k("customer", 77): REF,
        k("customer_address", 1): OWNED,
        k("customer_address", 2): OWNED,
        k("loyalty_account", 42, cols=("customer_id",)): OWNED,
        k("purchase_order", 1001): OWNED,
        k("purchase_order", 1002): OWNED,
        k("order_line", 1001, 1, cols=("order_id", "line_no")): OWNED,
        k("order_line", 1001, 2, cols=("order_id", "line_no")): OWNED,
        k("order_line", 1002, 1, cols=("order_id", "line_no")): OWNED,
        k("shipment", 1): OWNED,
        k("shipment", 2): OWNED,
        k("payment", 1): OWNED,
        k("payment", 2): OWNED,
        k("support_ticket", 10): OWNED,
        k("ticket_message", 100): OWNED,
        k("ticket_message", 101): SHARED,  # by customer 77 on 42's ticket
        k("ticket_message", 102): SHARED,  # by 42 on 77's ticket
        k("ticket_message", 103): OWNED,  # staff message (NULL author) on 42's ticket
        k("message_reaction", 1000): SHARED,  # reached only through SHARED message 101
        k("message_reaction", 1001): OWNED,
        k("message_reaction", 1002): SHARED,
        k("consent_log", 42, cols=("customer_id",), pred=True): OWNED,
        k("document", 500): OWNED,
        k("document_embedding", 900): OWNED,
        k("customer_profile_cache", 42, cols=("customer_id",)): OWNED,
        k("customer_ltv", 42, cols=("customer_id",), pred=True): OWNED,
        k("customer_segment", 42, cols=("customer_id",)): OWNED,
    }


def _run(demo, key=(42,), limits=None, **reader_kw):
    return discover_records(
        demo.graph, demo.root, key, demo.reader(**reader_kw), limits or DiscoveryLimits()
    )


def test_ground_truth_records_and_roles(kit, demo):
    result = _run(demo)
    got = {k: a.role for k, a in result.record_graph.nodes()}
    assert got == _expected(kit)


def test_precision_and_recall_are_one(kit, demo):
    expected = set(_expected(kit))
    got = {k for k, _ in _run(demo).record_graph.nodes()}
    tp = len(expected & got)
    assert tp / len(got) == 1.0 and tp / len(expected) == 1.0


def test_upstream_and_unrelated_rows_are_not_reached(kit, demo):
    got = {k for k, _ in _run(demo).record_graph.nodes()}
    for forbidden in (
        _k(kit, "customer", 5),  # referrer of 42 (upstream)
        _k(kit, "customer", 99),
        _k(kit, "product", 7),  # order_line -> product is upstream
        _k(kit, "purchase_order", 2001),  # belongs to 77; 77 is never traversed
        _k(kit, "support_ticket", 20),
        _k(kit, "ticket_message", 104),
        _k(kit, "shipment", 4),  # dangling composite reference
    ):
        assert forbidden not in got
    assert not [k for k in got if k.table == kit.t("marketing_event_log")]
    assert not [k for k in got if k.table == kit.t("daily_revenue")]


def test_affected_artifacts(kit, demo):
    result = _run(demo)
    assert dict(result.affected_artifacts) == {
        kit.t("customer_ltv"): Traceability.AGGREGATE,  # AGGREGATE beats KEY_PRESERVING
        kit.t("customer_profile_cache"): Traceability.KEY_PRESERVING,
        kit.t("customer_segment"): Traceability.KEY_PRESERVING,
        kit.t("daily_revenue"): Traceability.AGGREGATE,
        kit.t("document_embedding"): Traceability.KEY_PRESERVING,
    }
    assert list(result.affected_artifacts) == sorted(result.affected_artifacts)


def test_depths_edges_and_composite_traversal(kit, demo):
    rg = _run(demo).record_graph
    attrs = dict(rg.nodes())
    assert attrs[_k(kit, "customer", 42)].depth == 0
    assert attrs[_k(kit, "shipment", 1)].depth == 3
    assert attrs[_k(kit, "ticket_message", 100)].depth == 1  # via author FK
    ship_edge = next(
        e
        for e in demo.fk_edges
        if e.constraint_name == "shipment_order_line_fkey" and e.level is LineageLevel.TABLE
    )
    assert (
        _k(kit, "order_line", 1001, 1, cols=("order_id", "line_no")),
        _k(kit, "shipment", 1),
        ship_edge.edge_id,
    ) in rg.edges()
    # message 100 is reached twice (ticket FK and author FK)
    assert len(attrs[_k(kit, "ticket_message", 100)].via_edge_ids) == 2
    order = rg.deletion_order(lambda _eid: True).order
    assert order.index(_k(kit, "shipment", 1)) < order.index(
        _k(kit, "order_line", 1001, 1, cols=("order_id", "line_no"))
    )


def test_warnings_flag_predicate_tables(demo):
    w = _run(demo).warnings
    assert "app.consent_log has no primary key; rows are addressed by predicate" in w
    assert list(w) == sorted(set(w))


def test_deterministic_across_runs_order_and_batching(demo):
    base = _run(demo)
    shuffled = _run(demo, shuffle_seed=7)
    tiny = _run(demo, limits=DiscoveryLimits(batch_size=1))
    for other in (shuffled, tiny):
        assert other.record_graph.record_set_hash() == base.record_graph.record_set_hash()
        assert other.record_graph.nodes() == base.record_graph.nodes()
        assert other.record_graph.edges() == base.record_graph.edges()
        assert dict(other.affected_artifacts) == dict(base.affected_artifacts)


def test_batches_respect_batch_size(demo):
    reader = demo.reader()
    discover_records(demo.graph, demo.root, (42,), reader, DiscoveryLimits(batch_size=1))
    assert reader.calls and all(c["n"] <= 1 for c in reader.calls)
    reader2 = demo.reader()
    discover_records(demo.graph, demo.root, (42,), reader2, DiscoveryLimits(batch_size=2))
    assert all(c["n"] <= 2 for c in reader2.calls)
    assert len(reader2.calls) < len(reader.calls)


def test_record_set_fingerprint(demo):
    fp = record_set_fingerprint(demo.graph, demo.root, (42,), demo.reader(), DiscoveryLimits())
    assert fp == _run(demo).record_graph.record_set_hash()
    demo.data[demo.root.table].append(
        {"id": 123, "email": "x", "full_name": "x", "phone": None, "referrer_id": 42}
    )
    assert (
        record_set_fingerprint(demo.graph, demo.root, (42,), demo.reader(), DiscoveryLimits()) != fp
    )


def test_subject_not_found(demo):
    with pytest.raises(SubjectNotFoundError) as e:
        _run(demo, key=(4242,))
    assert e.value.details == {"table": "app.customer"}
    assert "4242" not in e.value.message


@pytest.mark.parametrize(
    ("limits", "name"),
    [
        (DiscoveryLimits(max_records=5), "max_records"),
        (DiscoveryLimits(max_depth=1), "max_depth"),
        (DiscoveryLimits(max_depth=2), "max_depth"),
    ],
)
def test_limits(demo, limits, name):
    with pytest.raises(DiscoveryLimitExceeded) as e:
        _run(demo, limits=limits)
    assert e.value.details["limit"] == name
    assert set(e.value.details) == {"limit", "value", "table"}


def test_limits_exactly_at_size_pass(kit, demo):
    n = len(_expected(kit))
    assert len(_run(demo, limits=DiscoveryLimits(max_records=n, max_depth=3)).record_graph) == n


def test_invalid_root_definitions(kit, demo):
    with pytest.raises(ValidationError):
        _run(demo, key=(42, 1))
    bad_root = SubjectRoot(kit.uid("r"), kit.t("customer"), ("email",))
    with pytest.raises(ValidationError):
        discover_records(demo.graph, bad_root, ("x",), demo.reader(), DiscoveryLimits())
    no_pk = SubjectRoot(kit.uid("r"), kit.t("consent_log"), ("customer_id",))
    with pytest.raises(ValidationError):
        discover_records(demo.graph, no_pk, (42,), demo.reader(), DiscoveryLimits())


def _hidden_copy_edge(kit, status: EdgeStatus) -> RelationshipEdge:
    return RelationshipEdge(
        edge_id=UUID("00000000-0000-0000-0000-00000000e0e0"),
        level=LineageLevel.TABLE,
        origin=EdgeOrigin.INFERRED,
        source=kit.t("customer"),
        target=kit.t("marketing_event_log"),
        source_columns=("email",),
        target_columns=("email",),
        transform=Transform.FOREIGN_KEY,
        traceability=Traceability.KEY_PRESERVING,
        status=status,
        confidence=0.9,
    )


def test_suggested_edges_are_never_traversed(kit, demo):
    d = demo.with_edges([_hidden_copy_edge(kit, EdgeStatus.SUGGESTED)])
    result = _run(d)
    assert not [
        k for k, _ in result.record_graph.nodes() if k.table == kit.t("marketing_event_log")
    ]
    assert any("unconfirmed" in w for w in result.warnings)


def test_confirmed_inferred_edge_is_traversed(kit, demo):
    d = demo.with_edges([_hidden_copy_edge(kit, EdgeStatus.CONFIRMED)])
    d.data[kit.t("marketing_event_log")].append(
        {"email": " Alice.Subject@Example.ORG ", "event": "open"}
    )
    got = dict(_run(d).record_graph.nodes())
    mel = [k for k in got if k.table == kit.t("marketing_event_log")]
    assert len(mel) == 1 and mel[0].is_predicate and mel[0].columns == ("email",)
    assert got[mel[0]].role is OWNED  # not a declared reference, so no SHARED check


def test_opaque_and_keyless_edges_mark_artifacts(kit, demo):
    opaque = RelationshipEdge(
        edge_id=UUID("00000000-0000-0000-0000-00000000e0e1"),
        level=LineageLevel.TABLE,
        origin=EdgeOrigin.SQL_LINEAGE,
        source=kit.t("purchase_order"),
        target=kit.t("daily_revenue"),
        source_columns=(),
        target_columns=(),
        transform=Transform.EXPRESSION,
        traceability=Traceability.OPAQUE,
        status=EdgeStatus.CONFIRMED,
        artifact_id=kit.uid("x"),
    )
    keyless = RelationshipEdge(
        edge_id=UUID("00000000-0000-0000-0000-00000000e0e2"),
        level=LineageLevel.TABLE,
        origin=EdgeOrigin.SQL_LINEAGE,
        source=kit.t("document"),
        target=kit.t("customer_segment"),
        source_columns=(),
        target_columns=(),
        transform=Transform.IDENTITY,
        traceability=Traceability.KEY_PRESERVING,
        status=EdgeStatus.CONFIRMED,
        artifact_id=kit.uid("y"),
    )
    result = _run(demo.with_edges([opaque, keyless]))
    assert result.affected_artifacts[kit.t("daily_revenue")] is Traceability.OPAQUE
    assert result.affected_artifacts[kit.t("customer_segment")] is Traceability.OPAQUE
    assert any("without key columns" in w for w in result.warnings)


def test_self_referencing_subject_stays_root(kit, demo):
    for row in demo.data[kit.t("customer")]:
        if row["id"] == 42:
            row["referrer_id"] = 42
    rg = _run(demo).record_graph
    assert rg.attrs(_k(kit, "customer", 42)).role is ROOT
    assert rg.deletion_order(lambda _e: True).break_edges == ()


def test_shared_cascades_through_descendants(kit, demo):
    # 42 writes on 77's ticket; a reaction on that message is shared too, and so is a
    # reaction-of-reaction chain via message_reaction -> (no further tables) -> stays SHARED
    result = dict(_run(demo).record_graph.nodes())
    assert result[_k(kit, "message_reaction", 1002)].role is SHARED
    assert result[_k(kit, "message_reaction", 1000)].role is SHARED
    assert result[_k(kit, "message_reaction", 1001)].role is OWNED


def test_other_subject_rows_reached_as_subject_are_not_traversed(kit, demo):
    # customer 77's own rows must not appear although 77 is in the record graph
    got = {k for k, _ in _run(demo).record_graph.nodes()}
    assert _k(kit, "customer_address", 3) not in got
    assert _k(kit, "document", 501) not in got
    assert _k(kit, "customer_profile_cache", 77, cols=("customer_id",)) not in got


def test_policies_argument_is_accepted(demo):
    from lae.contracts.types import PolicySet

    r = discover_records(
        demo.graph, demo.root, (42,), demo.reader(), DiscoveryLimits(), PolicySet()
    )
    assert len(r.record_graph) == len(_run(demo).record_graph)


def test_uuid_and_decimal_key_values_are_normalised(kit):
    from decimal import Decimal

    from lae.discovery import build_catalog, foreign_key_edges
    from lae.lineage import build_lineage_graph

    tables = [
        kit.table("acct", [("id", "uuid")], ("id",)),
        kit.table("item", [("id", "numeric"), ("acct_id", "uuid"), ("seen", "date")], ("id",)),
    ]
    fks = [kit.fk("item_acct_fkey", "item", ("acct_id",), "acct", ("id",))]
    cat = build_catalog(tables, fks, ["app"])
    graph = build_lineage_graph(cat, foreign_key_edges(fks, cat))
    a = UUID("12345678-1234-5678-1234-567812345678")
    data = {
        kit.t("acct"): [{"id": a}],
        kit.t("item"): [{"id": Decimal("7"), "acct_id": a, "seen": "2026-01-01"}],
    }
    reader = kit.FakeTargetReader(cat, data)
    root = SubjectRoot(kit.uid("r"), kit.t("acct"), ("id",))
    rg = discover_records(graph, root, (str(a),), reader, DiscoveryLimits()).record_graph
    assert RecordKey(kit.t("item"), ("id",), (7,)) in rg
