"""Planner rules on the demo shop scenario and small purpose-built schemas."""

from __future__ import annotations

import re
from uuid import UUID

import pytest
from erasure_harness import (
    PLAN_ID,
    REQUEST_ID,
    FakeDB,
    Schema,
    T,
    codes,
    discover,
    items_of,
    make_plan,
    marketing_counts,
    shop,
    uid,
)

from lae.contracts.enums import (
    ActionCategory,
    ConflictCode,
    EdgeOrigin,
    ErasureStrategy,
    LineageLevel,
    OnDeleteAction,
    PlanAction,
    RecordRole,
    RetentionAction,
    Severity,
)
from lae.contracts.types import ColumnRef, PolicySet, RecordKey
from lae.errors import GraphIntegrityError
from lae.lineage import RecordDiscoveryResult, RecordGraph
from lae.planner import build_plan, detect_conflicts, pseudonym_columns
from lae.planner.text import edge_label

pytestmark = pytest.mark.unit

A = PlanAction


def keyset(plan, action, table):  # type: ignore[no-untyped-def]
    return {k for i in items_of(plan, action, table) for k in i.record_keys}


def all_keys(plan, table):  # type: ignore[no-untyped-def]
    return {k for i in plan.items if i.table == T(table) for k in i.record_keys}


# ---------------------------------------------------------------------- default policy


def test_shop_default_plan_structure() -> None:
    sh = shop()
    plan = make_plan(sh.schema, sh.db, predicate_row_counts=marketing_counts(sh.db))
    assert [i.step_order for i in plan.items] == list(range(1, len(plan.items) + 1))
    assert keyset(plan, A.DELETE_ROWS, "customer") == {(42,)}
    assert keyset(plan, A.DELETE_ROWS, "purchase_order") == {(1001,), (1002,)}
    assert keyset(plan, A.DELETE_ROWS, "payment") == {(601,)}
    assert keyset(plan, A.DELETE_ROWS, "shipment") == {(501,)}
    assert keyset(plan, A.DELETE_ROWS, "support_ticket") == {(301,)}
    # cascades are explicit EXPECT_CASCADE steps, not deletes
    assert keyset(plan, A.EXPECT_CASCADE, "customer_address") == {(1,), (2,)}
    assert keyset(plan, A.EXPECT_CASCADE, "order_line") == {(1001, 1), (1001, 2), (1002, 1)}
    assert keyset(plan, A.EXPECT_CASCADE, "ticket_message") == {(401,)}
    assert not items_of(plan, A.DELETE_ROWS, "order_line")
    (ol,) = items_of(plan, A.EXPECT_CASCADE, "order_line")
    assert ol.key_columns == ("order_id", "line_no") and ol.expected_row_count == 3
    # the predicate delete uses the supplied row count
    (mk,) = items_of(plan, A.DELETE_ROWS, "marketing_event_log")
    assert mk.key_columns == ("customer_id",) and mk.expected_row_count == 2
    assert all(i.category is not None for i in plan.items)


def test_other_subjects_are_never_deleted() -> None:
    sh = shop()
    plan = make_plan(sh.schema, sh.db)
    (nul,) = items_of(plan, A.NULLIFY_REFERENCE, "customer")
    assert nul.record_keys == ((77,),) and nul.target_columns == ("referrer_id",)
    assert nul.expected_row_count == 1
    for table in ("customer", "customer_address", "purchase_order", "order_line", "payment"):
        for i in plan.items:
            if i.table == T(table) and i.category is ActionCategory.DELETE:
                assert (77,) not in i.record_keys and (99,) not in i.record_keys
    assert (2001,) not in all_keys(plan, "purchase_order")
    assert not all_keys(plan, "product")  # upstream table is never traversed


def test_root_table_protection_overrides_owned_role() -> None:
    sh = shop()
    disc = sh.discover()
    rg = disc.record_graph
    # a buggy discovery marks another customer as OWNED: the planner must not delete it
    other = RecordKey(T("customer"), ("id",), (99,))
    rg.add_dependency(
        RecordKey(T("customer"), ("id",), (42,)),
        other,
        uid("fk_customer_referrer"),
        RecordRole.OWNED,
    )
    plan = build_plan(
        REQUEST_ID, disc, sh.lineage(), sh.schema.policies(), sh.artifacts, plan_id=PLAN_ID
    )
    assert (99,) not in keyset(plan, A.DELETE_ROWS, "customer")
    assert (99,) in keyset(plan, A.NULLIFY_REFERENCE, "customer")


def test_not_null_shared_record_is_blocking_and_cascade_flagged() -> None:
    """Policy §2(b)/(c): message 402 (customer 77) on the subject ticket, ticket_id NOT NULL."""
    sh = shop()
    plan = make_plan(sh.schema, sh.db)
    (mr,) = items_of(plan, A.MANUAL_REVIEW, "ticket_message")
    assert mr.record_keys == ((402,),) and mr.lineage_level is LineageLevel.RECORD
    shared = next(c for c in plan.conflicts if c.code is ConflictCode.SHARED_RECORD)
    assert shared.severity is Severity.BLOCKING and not shared.resolved
    cascade = next(c for c in plan.conflicts if c.code is ConflictCode.CASCADE_DESTROYS_RETAINED)
    assert cascade.table == T("ticket_message") and cascade.severity is Severity.BLOCKING
    assert "cascade would delete a record not owned by the subject" in cascade.message
    assert plan.is_blocked
    deleted = {k for i in plan.items if i.category is ActionCategory.DELETE for k in i.record_keys}
    assert (402,) not in deleted


def test_nullable_shared_record_is_detached_before_parent_delete() -> None:
    """Policy §2(a): the shared message is detached (NULLIFY) before its ticket is deleted."""
    sh = shop(ticket_nullable=True)
    plan = make_plan(sh.schema, sh.db)
    (nul,) = items_of(plan, A.NULLIFY_REFERENCE, "ticket_message")
    assert nul.record_keys == ((402,),) and nul.target_columns == ("ticket_id",)
    assert "shared rows" in nul.reason
    (ticket,) = items_of(plan, A.DELETE_ROWS, "support_ticket")
    assert nul.step_order < ticket.step_order and nul.step_order in ticket.depends_on
    # the cascade check of the ticket batch expects only the subject's own message (401)
    assert "cascade_check fk_tm_ticket: app.ticket_message(ticket_id) <- (id) = 1" in ticket.reason
    assert not items_of(plan, A.MANUAL_REVIEW)
    c = next(c for c in plan.conflicts if c.code is ConflictCode.SHARED_RECORD)
    assert c.severity is Severity.WARNING and "set to NULL first" in c.message
    assert ConflictCode.CASCADE_DESTROYS_RETAINED not in codes(plan)
    assert not plan.is_blocked


def test_shared_row_with_not_null_restrict_link_is_blocking() -> None:
    s = Schema()
    s.table("customer", "id")
    s.table("thread", "id", "customer_id", not_null=("customer_id",))
    s.table("post", "id", "thread_id", "author_id", not_null=("thread_id",))
    s.fk(
        "fk_thread_customer", "thread", ["customer_id"], "customer", ["id"], OnDeleteAction.CASCADE
    )
    s.fk("fk_post_thread", "post", ["thread_id"], "thread", ["id"], OnDeleteAction.RESTRICT)
    s.fk("fk_post_author", "post", ["author_id"], "customer", ["id"], OnDeleteAction.SET_NULL)
    db = FakeDB(s)
    db.insert("customer", id=1)
    db.insert("customer", id=2)
    db.insert("thread", id=5, customer_id=1)
    db.insert("post", id=9, thread_id=5, author_id=2)
    plan = make_plan(s, db, subject=(1,))
    blocking = {c.code for c in plan.conflicts if c.severity is Severity.BLOCKING}
    assert blocking == {ConflictCode.SHARED_RECORD}
    assert items_of(plan, A.MANUAL_REVIEW, "post")


def test_no_shared_record_gives_unblocked_plan() -> None:
    sh = shop(shared_message=False)
    plan = make_plan(sh.schema, sh.db)
    assert not plan.is_blocked
    assert ConflictCode.NO_PRIMARY_KEY in codes(plan)


def test_predicate_key_without_counts_defaults_to_one_per_predicate() -> None:
    sh = shop()
    plan = make_plan(sh.schema, sh.db)
    (mk,) = items_of(plan, A.DELETE_ROWS, "marketing_event_log")
    assert mk.expected_row_count == 1
    assert "no primary key" in mk.reason
    c = next(c for c in plan.conflicts if c.code is ConflictCode.NO_PRIMARY_KEY)
    assert c.severity is Severity.WARNING and c.table == T("marketing_event_log")


def test_deletes_children_before_parents_in_shop() -> None:
    sh = shop()
    plan = make_plan(sh.schema, sh.db)
    step = {i.table.name: i.step_order for i in items_of(plan, A.DELETE_ROWS)}
    assert step["shipment"] < step["purchase_order"] < step["customer"]
    assert step["payment"] < step["purchase_order"]
    # ticket_message(401) cascades from the ticket and references the customer (NO ACTION):
    # the ticket must be deleted before the customer
    assert step["support_ticket"] < step["customer"]
    (cust,) = items_of(plan, A.DELETE_ROWS, "customer")
    nul = items_of(plan, A.NULLIFY_REFERENCE, "customer")[0]
    assert nul.step_order in cust.depends_on
    assert step["purchase_order"] in cust.depends_on
    for i in items_of(plan, A.EXPECT_CASCADE):
        assert i.depends_on and all(d < i.step_order for d in i.depends_on)
        assert i.step_order > max(step.values())


def test_reasons_are_pii_free_and_explainable() -> None:
    sh = shop()
    plan = make_plan(sh.schema, sh.db)
    raw = [
        "alice",
        "Alice",
        "bob@",
        "carol",
        "example.com",
        "1001",
        "1002",
        "601",
        "501",
        "301",
        "401",
        "801",
    ]
    for i in plan.items:
        assert i.reason
        text = re.sub(r"definition_sha256=[0-9a-f]{64}", "", i.reason)
        for r in raw:
            assert r not in text, (r, i.reason)
    for c in plan.conflicts:
        for r in raw:
            assert r not in c.message
    (po,) = items_of(plan, A.DELETE_ROWS, "purchase_order")
    assert po.reason == (
        "DELETE 2 rows of app.purchase_order: owned by subject via FK fk_po_customer "
        "(ON DELETE RESTRICT); cascade_check fk_ol_order: app.order_line(order_id) <- (id) = 3"
    )
    (root,) = items_of(plan, A.DELETE_ROWS, "customer")
    assert root.reason == (
        "DELETE 1 row of app.customer: subject root; cascade_check fk_address_customer: "
        "app.customer_address(customer_id) <- (id) = 2"
    )
    assert root.reason_edge_ids == ()
    assert po.reason_edge_ids == (uid("fk_po_customer"),)


def test_edge_label_variants() -> None:
    sh = shop()
    lg = sh.lineage()
    assert edge_label(lg.edge(uid("fk_ol_order"))) == "FK fk_ol_order (ON DELETE CASCADE)"
    assert edge_label(lg.edge(uid("agg_revenue"))) == "SQL_LINEAGE agg_revenue"
    assert edge_label(lg.edge(uid("map_cache"))) == "REGISTERED_MAPPING map_cache (customer_id)"


# ---------------------------------------------------------------------- ANONYMIZE / RETAIN


def test_anonymize_targets_pii_and_fk_to_deleted_rows() -> None:
    sh = shop(shared_message=False)
    sh.schema.policy("purchase_order", RetentionAction.ANONYMIZE)
    plan = make_plan(sh.schema, sh.db)
    (an,) = items_of(plan, A.ANONYMIZE_ROWS, "purchase_order")
    assert an.target_columns == ("customer_id", "shipping_name")
    assert an.record_keys == ((1001,), (1002,)) and an.expected_row_count == 2
    assert "shipping_name (PSEUDONYMIZE)" in an.reason
    assert "customer_id (NULLIFY: references rows being deleted)" in an.reason
    # anonymized orders are not deleted; their lines (policy DELETE) are deleted explicitly
    assert not keyset(plan, A.DELETE_ROWS, "purchase_order")
    assert keyset(plan, A.DELETE_ROWS, "order_line") == {(1001, 1), (1001, 2), (1002, 1)}
    step = {i.table.name: i.step_order for i in items_of(plan, A.DELETE_ROWS)}
    assert step["shipment"] < step["order_line"]
    assert an.step_order < min(step.values())
    (cust,) = items_of(plan, A.DELETE_ROWS, "customer")
    assert an.step_order in cust.depends_on
    # pseudonymization is not anonymization
    assert ConflictCode.RETAINED_WITH_PII in codes(plan)
    assert not plan.is_blocked
    cols = pseudonym_columns(plan, sh.schema.policies(), sh.lineage())
    assert cols == frozenset({ColumnRef(T("purchase_order"), "shipping_name")})


def test_pseudonym_columns_excludes_fk_columns() -> None:
    sh = shop(shared_message=False)
    sh.schema.policy("purchase_order", RetentionAction.ANONYMIZE)
    sh.schema.classify("purchase_order", "customer_id", ErasureStrategy.PSEUDONYMIZE)
    plan = make_plan(sh.schema, sh.db)
    cols = pseudonym_columns(plan, sh.schema.policies(), sh.lineage())
    assert ColumnRef(T("purchase_order"), "customer_id") not in cols


def test_anonymize_cascade_child_conflict() -> None:
    """AT-11 variant: retained rows under a NOT NULL CASCADE FK would be destroyed."""
    sh = shop(shared_message=False)
    sh.schema.policy("order_line", RetentionAction.ANONYMIZE)
    plan = make_plan(sh.schema, sh.db)
    c = next(c for c in plan.conflicts if c.code is ConflictCode.CASCADE_DESTROYS_RETAINED)
    assert c.table == T("order_line") and c.severity is Severity.BLOCKING
    assert plan.is_blocked


def test_anonymize_without_confirmed_pii_retains_rows() -> None:
    sh = shop(shared_message=False)
    sh.schema.policy("customer_address", RetentionAction.ANONYMIZE)
    # the address FK is NOT NULL CASCADE: conflict, and no column can be scrubbed
    plan = make_plan(sh.schema, sh.db)
    assert ConflictCode.CASCADE_DESTROYS_RETAINED in codes(plan)
    (ret,) = items_of(plan, A.RETAIN_ROWS, "customer_address")
    assert "no confirmed PII column" in ret.reason


def test_unconfirmed_and_unscrubbable_pii_warnings() -> None:
    sh = shop(shared_message=False)
    sh.schema.policy("customer", RetentionAction.ANONYMIZE)
    sh.schema.classify("customer", "referrer_id", confirmed=False)
    plan = make_plan(sh.schema, sh.db)
    (an,) = items_of(plan, A.ANONYMIZE_ROWS, "customer")
    # email is NOT NULL with strategy NULLIFY: it cannot be scrubbed and stays
    assert an.target_columns == ("name",)
    msgs = [c.message for c in plan.conflicts]
    assert any("email" in m and "cannot be scrubbed" in m for m in msgs)
    unc = next(c for c in plan.conflicts if c.code is ConflictCode.UNCONFIRMED_PII)
    assert unc.severity is Severity.WARNING and "referrer_id" in unc.message
    # the anonymized root is not deleted, so nothing references a deleted customer
    assert not keyset(plan, A.DELETE_ROWS, "customer")


def test_retain_nullable_restrict_gives_nullify_and_retain_rows() -> None:
    sh = shop(shared_message=False)
    sh.schema.policy("purchase_order", RetentionAction.RETAIN)
    plan = make_plan(sh.schema, sh.db)
    nul = items_of(plan, A.NULLIFY_REFERENCE, "purchase_order")
    assert len(nul) == 1 and nul[0].target_columns == ("customer_id",)
    assert "retained by policy RETAIN" in nul[0].reason
    (ret,) = items_of(plan, A.RETAIN_ROWS, "purchase_order")
    assert ret.record_keys == ((1001,), (1002,)) and ret.expected_row_count == 2
    assert nul[0].step_order in ret.depends_on
    assert ret.step_order > max(
        i.step_order for i in plan.items if i.category is ActionCategory.DELETE
    )
    w = next(c for c in plan.conflicts if c.code is ConflictCode.RETAINED_WITH_PII)
    assert "shipping_name" in w.message and w.severity is Severity.WARNING
    assert not plan.is_blocked


def test_retain_not_null_restrict_is_blocking() -> None:
    sh = shop(shared_message=False)
    sh.schema.policy("payment", RetentionAction.RETAIN)
    plan = make_plan(sh.schema, sh.db)
    c = next(c for c in plan.conflicts if c.code is ConflictCode.RESTRICT_FROM_RETAINED)
    assert c.table == T("payment") and c.severity is Severity.BLOCKING and plan.is_blocked


# ---------------------------------------------------------------------- small schemas


def _people(referrer_nullable: bool, on_delete: OnDeleteAction) -> tuple[Schema, FakeDB]:
    s = Schema()
    nn = () if referrer_nullable else ("referrer_id",)
    s.table("customer", "id", "referrer_id", not_null=nn)
    s.fk("fk_ref", "customer", ["referrer_id"], "customer", ["id"], on_delete)
    db = FakeDB(s)
    db.insert("customer", id=1, referrer_id=None if referrer_nullable else 1)
    db.insert("customer", id=2, referrer_id=1)
    return s, db


def test_not_null_reference_from_other_subject() -> None:
    s, db = _people(False, OnDeleteAction.NO_ACTION)
    plan = make_plan(s, db, subject=(1,))
    c = next(c for c in plan.conflicts if c.code is ConflictCode.NOT_NULL_REFERENCE)
    assert c.table == T("customer") and plan.is_blocked
    assert not items_of(plan, A.NULLIFY_REFERENCE)


def test_cascade_onto_other_subject_is_blocking() -> None:
    s, db = _people(False, OnDeleteAction.CASCADE)
    plan = make_plan(s, db, subject=(1,))
    assert ConflictCode.CASCADE_DESTROYS_RETAINED in codes(plan)


def test_set_default_not_null_reference_on_retained_row() -> None:
    s = Schema()
    s.table("customer", "id")
    s.table("note", "id", "customer_id", not_null=("customer_id",))
    s.fk("fk_note", "note", ["customer_id"], "customer", ["id"], OnDeleteAction.SET_DEFAULT)
    s.policy("note", RetentionAction.RETAIN)
    db = FakeDB(s)
    db.insert("customer", id=1)
    db.insert("note", id=5, customer_id=1)
    plan = make_plan(s, db, subject=(1,))
    assert ConflictCode.NOT_NULL_REFERENCE in codes(plan)


def _cycle(peer_nullable: bool) -> tuple[Schema, FakeDB]:
    s = Schema()
    s.table("customer", "id")
    s.table(
        "node",
        "id",
        "customer_id",
        "peer_id",
        not_null=("customer_id",) + (() if peer_nullable else ("peer_id",)),
    )
    s.table("other", "id", "node_id", not_null=("node_id",))
    s.fk("fk_node_customer", "node", ["customer_id"], "customer", ["id"], OnDeleteAction.NO_ACTION)
    s.fk("fk_other_node", "other", ["node_id"], "node", ["id"], OnDeleteAction.NO_ACTION)
    s.fk("fk_node_peer", "node", ["peer_id"], "other", ["id"], OnDeleteAction.NO_ACTION)
    db = FakeDB(s)
    db.insert("customer", id=1)
    db.insert("node", id=10, customer_id=1, peer_id=20)
    db.insert("other", id=20, node_id=10)
    return s, db


def test_unbreakable_cycle_conflict() -> None:
    s, db = _cycle(peer_nullable=False)
    plan = make_plan(s, db, subject=(1,))
    c = next(c for c in plan.conflicts if c.code is ConflictCode.UNBREAKABLE_CYCLE)
    assert c.severity is Severity.BLOCKING and plan.is_blocked
    assert "app.node" in c.message and "app.other" in c.message
    # still a complete plan to review
    assert {i.table.name for i in items_of(plan, A.DELETE_ROWS)} == {"customer", "node", "other"}


def test_breakable_cycle_nullifies_first() -> None:
    s, db = _cycle(peer_nullable=True)
    plan = make_plan(s, db, subject=(1,))
    assert not plan.is_blocked
    (nul,) = items_of(plan, A.NULLIFY_REFERENCE, "node")
    assert nul.target_columns == ("peer_id",) and "cycle" in nul.reason
    step = {i.table.name: i.step_order for i in items_of(plan, A.DELETE_ROWS)}
    assert nul.step_order < step["other"] < step["node"] < step["customer"]


def test_pure_cascade_cycle_deletes_one_row_explicitly() -> None:
    s = Schema()
    s.table("customer", "id")
    s.table("a", "id", "customer_id", "b_id", not_null=("customer_id",))
    s.table("b", "id", "a_id")
    s.fk("fk_a_customer", "a", ["customer_id"], "customer", ["id"], OnDeleteAction.SET_NULL)
    s.fk("fk_b_a", "b", ["a_id"], "a", ["id"], OnDeleteAction.CASCADE)
    s.fk("fk_a_b", "a", ["b_id"], "b", ["id"], OnDeleteAction.CASCADE)
    db = FakeDB(s)
    db.insert("customer", id=1)
    db.insert("a", id=10, customer_id=1, b_id=20)
    db.insert("b", id=20, a_id=10)
    plan = make_plan(s, db, subject=(1,))
    # a(10) is reached via SET NULL (not cascade); b(20) cascades from a(10); a(10) also
    # cascades from b(20): a cascade cycle; at least one of them is deleted explicitly
    deleted = {(i.table.name, k) for i in items_of(plan, A.DELETE_ROWS) for k in i.record_keys}
    assert ("a", (10,)) in deleted or ("b", (20,)) in deleted


def test_table_level_cycle_without_record_cycle_uses_ranked_batches() -> None:
    s = Schema()
    s.table("customer", "id")
    s.table("a", "id", "customer_id", "b_id", not_null=("customer_id",))
    s.table("b", "id", "customer_id", "a_id", not_null=("customer_id",))
    s.fk("fk_a_customer", "a", ["customer_id"], "customer", ["id"], OnDeleteAction.CASCADE)
    s.fk("fk_b_customer", "b", ["customer_id"], "customer", ["id"], OnDeleteAction.CASCADE)
    s.fk("fk_a_b", "a", ["b_id"], "b", ["id"], OnDeleteAction.NO_ACTION)
    s.fk("fk_b_a", "b", ["a_id"], "a", ["id"], OnDeleteAction.NO_ACTION)
    s.policy("customer", RetentionAction.ANONYMIZE)  # root kept, so a/b rows are explicit
    db = FakeDB(s)
    db.insert("customer", id=1)
    # a1 -> b1 -> a2 (record chain), so table a and b reference each other
    db.insert("a", id=1, customer_id=1, b_id=None)
    db.insert("b", id=1, customer_id=1, a_id=1)
    db.insert("a", id=2, customer_id=1, b_id=1)
    plan = make_plan(s, db, subject=(1,))
    dels = items_of(plan, A.DELETE_ROWS)
    order = [(i.table.name, i.record_keys) for i in dels]
    assert order == [("a", ((2,),)), ("b", ((1,),)), ("a", ((1,),))]
    assert dels[1].depends_on == (dels[0].step_order,)


# ---------------------------------------------------------------------- integrity errors


def test_record_in_unknown_table_raises_graph_integrity() -> None:
    sh = shop()
    rg = RecordGraph()
    rg.add_root(RecordKey(T("nope"), ("id",), (1,)))
    disc = RecordDiscoveryResult(rg, {}, ())
    with pytest.raises(GraphIntegrityError) as ei:
        build_plan(REQUEST_ID, disc, sh.lineage(), PolicySet(), [], plan_id=PLAN_ID)
    assert ei.value.code == "UNKNOWN_RECORD_TABLE"


def test_record_edge_mismatch_raises_graph_integrity() -> None:
    sh = shop()
    rg = RecordGraph()
    root = RecordKey(T("customer"), ("id",), (42,))
    rg.add_root(root)
    rg.add_dependency(
        root, RecordKey(T("payment"), ("id",), (601,)), uid("fk_po_customer"), RecordRole.OWNED
    )
    with pytest.raises(GraphIntegrityError) as ei:
        build_plan(
            REQUEST_ID,
            RecordDiscoveryResult(rg, {}),
            sh.lineage(),
            PolicySet(),
            [],
            plan_id=PLAN_ID,
        )
    assert ei.value.code == "EDGE_MISMATCH"


def test_detect_conflicts_matches_build_plan() -> None:
    sh = shop()
    disc = sh.discover()
    plan = build_plan(
        REQUEST_ID, disc, sh.lineage(), sh.schema.policies(), sh.artifacts, plan_id=PLAN_ID
    )
    assert detect_conflicts(disc, sh.lineage(), sh.schema.policies(), sh.artifacts) == list(
        plan.conflicts
    )


def test_plan_ids_and_hashes_are_set() -> None:
    sh = shop()
    plan = make_plan(sh.schema, sh.db)
    assert plan.plan_id == PLAN_ID and plan.request_id == REQUEST_ID
    assert plan.schema_fingerprint == sh.schema.catalog().fingerprint
    assert (
        plan.record_set_hash
        == discover(sh.schema, sh.db, "customer", (42,)).record_graph.record_set_hash()
    )
    assert re.fullmatch(r"[0-9a-f]{64}", plan.plan_hash)
    assert isinstance(plan.plan_id, UUID)


def test_origin_inferred_edge_is_used_only_when_confirmed() -> None:
    sh = shop()
    lg = sh.lineage()
    assert lg.edge(uid("inf_marketing_customer")).origin is EdgeOrigin.INFERRED


def test_cascading_row_with_restricting_second_parent_is_deleted_explicitly() -> None:
    """Regression (found by Hypothesis): t1 cascades from customer but also references t0
    (RESTRICT), and t0 references customer (RESTRICT). Relying on the cascade would need
    customer before t0 and t0 before customer; deleting t1 explicitly first resolves it."""
    s = Schema()
    s.table("customer", "id")
    s.table("t0", "id", "customer_id", not_null=("customer_id",))
    s.table("t1", "id", "t0_id", "customer_id", not_null=("t0_id", "customer_id"))
    s.fk("fk_t0_customer", "t0", ["customer_id"], "customer", ["id"], OnDeleteAction.RESTRICT)
    s.fk("fk_t1_t0", "t1", ["t0_id"], "t0", ["id"], OnDeleteAction.RESTRICT)
    s.fk("fk_t1_customer", "t1", ["customer_id"], "customer", ["id"], OnDeleteAction.CASCADE)
    db = FakeDB(s)
    db.insert("customer", id=1)
    db.insert("t0", id=100, customer_id=1)
    db.insert("t1", id=200, t0_id=100, customer_id=1)
    plan = make_plan(s, db, subject=(1,))
    assert not plan.conflicts
    assert [i.table.name for i in items_of(plan, A.DELETE_ROWS)] == ["t1", "t0", "customer"]
    assert not items_of(plan, A.EXPECT_CASCADE)
