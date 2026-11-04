"""Derived artifacts (matviews, derived tables, embeddings, caches) and external artifacts."""

from __future__ import annotations

import hashlib

import pytest
from erasure_harness import (
    PLAN_ID,
    REQUEST_ID,
    SEGMENT_SQL,
    FakeDB,
    Schema,
    T,
    codes,
    items_of,
    make_plan,
    shop,
    uid,
)

from lae.contracts.enums import (
    ActionCategory,
    ArtifactKind,
    ConflictCode,
    EdgeOrigin,
    LineageLevel,
    OnDeleteAction,
    PlanAction,
    RefreshStrategy,
    Severity,
    TableKind,
    Traceability,
)
from lae.contracts.types import ArtifactDefinition, KeyMapping
from lae.lineage import RecordDiscoveryResult
from lae.planner import build_plan

pytestmark = pytest.mark.unit

A = PlanAction
BASE = frozenset({A.NULLIFY_REFERENCE, A.ANONYMIZE_ROWS, A.DELETE_ROWS, A.EXPECT_CASCADE})
DERIVED = frozenset(
    {A.DELETE_DERIVED_BY_KEY, A.INVALIDATE_CACHE, A.RECOMPUTE_TABLE, A.REFRESH_MATVIEW}
)


def test_matview_rows_give_refresh_never_delete() -> None:
    sh = shop()
    plan = make_plan(sh.schema, sh.db)
    assert not [
        i for i in plan.items if i.table == T("customer_ltv") and i.action is not A.REFRESH_MATVIEW
    ]
    (ref,) = items_of(plan, A.REFRESH_MATVIEW, "customer_ltv")
    assert ref.lineage_level is LineageLevel.TABLE and ref.expected_row_count is None
    assert ref.record_keys == () and ref.key_columns == ()
    assert "holds 1 row derived from the subject" in ref.reason
    assert uid("map_ltv") in ref.reason_edge_ids


def test_aggregate_artifact_is_refreshed() -> None:
    sh = shop()
    plan = make_plan(sh.schema, sh.db)
    (ref,) = items_of(plan, A.REFRESH_MATVIEW, "daily_revenue")
    assert "AGGREGATE lineage" in ref.reason
    (pay,) = items_of(plan, A.DELETE_ROWS, "payment")
    assert pay.step_order in ref.depends_on


def test_recompute_table_binds_definition_hash() -> None:
    sh = shop()
    plan = make_plan(sh.schema, sh.db)
    (rc,) = items_of(plan, A.RECOMPUTE_TABLE, "customer_segment")
    digest = hashlib.sha256(SEGMENT_SQL.encode()).hexdigest()
    assert rc.reason.endswith(f"definition_sha256={digest}")
    (ltv,) = items_of(plan, A.REFRESH_MATVIEW, "customer_ltv")
    assert ltv.step_order < rc.step_order and ltv.step_order in rc.depends_on


def test_embedding_and_cache_by_key() -> None:
    sh = shop()
    plan = make_plan(sh.schema, sh.db)
    (emb,) = items_of(plan, A.DELETE_DERIVED_BY_KEY, "document_embedding")
    assert emb.record_keys == ((801,),) and emb.expected_row_count == 1
    assert emb.category is ActionCategory.DELETE
    (cache,) = items_of(plan, A.INVALIDATE_CACHE, "customer_profile_cache")
    assert cache.record_keys == ((42,),) and cache.category is ActionCategory.INVALIDATE
    (ticket,) = items_of(plan, A.DELETE_ROWS, "support_ticket")
    assert ticket.step_order in emb.depends_on


def test_derived_steps_follow_base_steps_in_artifact_order() -> None:
    sh = shop()
    plan = make_plan(sh.schema, sh.db)
    base = [i.step_order for i in plan.items if i.action in BASE]
    derived = [i for i in plan.items if i.action in DERIVED]
    assert derived and min(i.step_order for i in derived) > max(base)
    keyed = [i.step_order for i in derived if i.lineage_level is LineageLevel.RECORD]
    rebuilt = [i for i in derived if i.lineage_level is LineageLevel.TABLE]
    assert max(keyed) < min(i.step_order for i in rebuilt)
    order = sh.lineage().artifact_order([i.table for i in rebuilt])
    assert [i.table for i in rebuilt] == order
    last = [
        i
        for i in plan.items
        if i.action in {A.RETAIN_ROWS, A.MANUAL_REVIEW, A.UNRESOLVED, A.SCHEDULE_EXTERNAL}
    ]
    assert min(i.step_order for i in last) > max(i.step_order for i in derived)


def test_propagates_refresh_to_downstream_artifacts_without_discovery_marks() -> None:
    sh = shop()
    disc = sh.discover()
    bare = RecordDiscoveryResult(disc.record_graph, {}, ())
    plan = build_plan(
        REQUEST_ID, bare, sh.lineage(), sh.schema.policies(), sh.artifacts, plan_id=PLAN_ID
    )
    (rev,) = items_of(plan, A.REFRESH_MATVIEW, "daily_revenue")
    assert "derived from app.payment, which this plan changes" in rev.reason


def _single(
    kind: ArtifactKind,
    strategy: RefreshStrategy,
    *,
    sql: str | None = "SELECT 1",
    table_kind: TableKind = TableKind.TABLE,
) -> tuple[Schema, FakeDB, ArtifactDefinition]:
    s = Schema()
    s.table("customer", "id")
    s.table("derived", "id", "customer_id", kind=table_kind)
    art = s.artifact("derived", kind, "derived", strategy, sql=sql)
    s.edge(
        "map_derived", "customer", ["id"], "derived", ["customer_id"], artifact_id=art.artifact_id
    )
    db = FakeDB(s)
    db.insert("customer", id=1)
    db.insert("derived", id=5, customer_id=1)
    return s, db, art


def test_derived_table_without_strategy_is_unresolved() -> None:
    s, db, _ = _single(ArtifactKind.DERIVED_TABLE, RefreshStrategy.NONE)
    plan = make_plan(s, db, subject=(1,))
    (un,) = items_of(plan, A.UNRESOLVED, "derived")
    assert un.lineage_level is LineageLevel.TABLE and "no usable refresh strategy" in un.reason
    c = next(c for c in plan.conflicts if c.code is ConflictCode.OPAQUE_ARTIFACT)
    assert c.severity is Severity.BLOCKING and plan.is_blocked


def test_recompute_without_sql_is_unresolved() -> None:
    s, db, _ = _single(ArtifactKind.DERIVED_TABLE, RefreshStrategy.RECOMPUTE_SQL, sql=None)
    plan = make_plan(s, db, subject=(1,))
    assert items_of(plan, A.UNRESOLVED, "derived")


def test_view_rows_need_no_step() -> None:
    s, db, _ = _single(ArtifactKind.VIEW, RefreshStrategy.NONE, table_kind=TableKind.VIEW)
    plan = make_plan(s, db, subject=(1,), affected={T("derived"): Traceability.AGGREGATE})
    assert not [i for i in plan.items if i.table == T("derived")]


def test_unregistered_matview_rows_are_refreshed() -> None:
    s = Schema()
    s.table("customer", "id")
    s.table("mv", "customer_id", pk=("customer_id",), kind=TableKind.MATERIALIZED_VIEW)
    s.edge("map_mv", "customer", ["id"], "mv", ["customer_id"], origin=EdgeOrigin.SQL_LINEAGE)
    db = FakeDB(s)
    db.insert("customer", id=1)
    db.insert("mv", customer_id=1)
    plan = make_plan(s, db, subject=(1,), artifacts=[])
    assert items_of(plan, A.REFRESH_MATVIEW, "mv") and not plan.is_blocked


def test_keyed_artifact_with_aggregate_lineage_is_unresolved() -> None:
    s, db, _ = _single(ArtifactKind.EMBEDDING_TABLE, RefreshStrategy.DELETE_BY_KEY)
    plan = make_plan(s, db, subject=(1,), affected={T("derived"): Traceability.AGGREGATE})
    assert items_of(plan, A.DELETE_DERIVED_BY_KEY, "derived")
    (un,) = items_of(plan, A.UNRESOLVED, "derived")
    assert "cannot be refreshed or recomputed" in un.reason


def test_opaque_artifact_is_unresolved_and_blocking() -> None:
    sh = shop(shared_message=False)
    plan = make_plan(sh.schema, sh.db, affected={T("daily_revenue"): Traceability.OPAQUE})
    (un,) = items_of(plan, A.UNRESOLVED, "daily_revenue")
    assert "OPAQUE" in un.reason and un.category is ActionCategory.UNRESOLVED
    assert ConflictCode.OPAQUE_ARTIFACT in codes(plan) and plan.is_blocked


def test_affected_artifact_missing_from_catalog_is_unresolved() -> None:
    sh = shop(shared_message=False)
    plan = make_plan(sh.schema, sh.db, affected={T("ghost"): Traceability.AGGREGATE})
    (un,) = items_of(plan, A.UNRESOLVED, "ghost")
    assert "not in the catalog" in un.reason


def test_shared_rows_in_keyed_artifact_are_kept() -> None:
    s = Schema()
    s.table("customer", "id")
    s.table("cache", "id", "customer_id", "owner_id")
    s.fk("fk_cache_owner", "cache", ["owner_id"], "customer", ["id"], OnDeleteAction.SET_NULL)
    art = s.artifact("cache", ArtifactKind.CACHE, "cache", RefreshStrategy.INVALIDATE_BY_KEY)
    s.edge("map_cache", "customer", ["id"], "cache", ["customer_id"], artifact_id=art.artifact_id)
    db = FakeDB(s)
    db.insert("customer", id=1)
    db.insert("customer", id=2)
    db.insert("cache", id=7, customer_id=1, owner_id=2)
    db.insert("cache", id=8, customer_id=1, owner_id=None)
    plan = make_plan(s, db, subject=(1,))
    # the shared cache row is never invalidated and not linked to deleted rows: kept, warned
    assert not items_of(plan, A.MANUAL_REVIEW)
    assert items_of(plan, A.INVALIDATE_CACHE, "cache")[0].record_keys == ((8,),)
    c = next(c for c in plan.conflicts if c.code is ConflictCode.SHARED_RECORD)
    assert c.severity is Severity.WARNING and "not modified" in c.message
    assert not plan.is_blocked


def test_artifact_cycle_is_unbreakable_cycle_conflict() -> None:
    s = Schema()
    s.table("customer", "id")
    s.table("mv_a", "x", pk=("x",), kind=TableKind.MATERIALIZED_VIEW)
    s.table("mv_b", "x", pk=("x",), kind=TableKind.MATERIALIZED_VIEW)
    s.edge(
        "a_b",
        "mv_a",
        [],
        "mv_b",
        [],
        origin=EdgeOrigin.SQL_LINEAGE,
        traceability=Traceability.AGGREGATE,
    )
    s.edge(
        "b_a",
        "mv_b",
        [],
        "mv_a",
        [],
        origin=EdgeOrigin.SQL_LINEAGE,
        traceability=Traceability.AGGREGATE,
    )
    db = FakeDB(s)
    db.insert("customer", id=1)
    affected = {T("mv_a"): Traceability.AGGREGATE, T("mv_b"): Traceability.AGGREGATE}
    plan = make_plan(s, db, subject=(1,), affected=affected)
    assert ConflictCode.UNBREAKABLE_CYCLE in codes(plan) and plan.is_blocked
    assert len(items_of(plan, A.REFRESH_MATVIEW)) == 2


# ---------------------------------------------------------------------- external artifacts


def _external(strategy: RefreshStrategy, cols: tuple[str, ...] = ("id",)) -> ArtifactDefinition:
    return ArtifactDefinition(
        artifact_id=uid("ext"),
        name="crm_contacts",
        kind=ArtifactKind.EXTERNAL,
        target=T("crm_contacts"),
        definition_sql=None,
        refresh_strategy=strategy,
        key_mappings=(KeyMapping(T("customer"), cols, ("contact_id",)),),
    )


def test_external_with_handler_is_scheduled_with_warning() -> None:
    sh = shop(shared_message=False)
    ext = _external(RefreshStrategy.EXTERNAL_TASK)
    plan = make_plan(sh.schema, sh.db, external=[ext])
    (sch,) = items_of(plan, A.SCHEDULE_EXTERNAL)
    assert sch.table == T("crm_contacts") and sch.record_keys == ((42,),)
    assert sch.key_columns == ("contact_id",) and sch.expected_row_count == 1
    assert sch.lineage_level is LineageLevel.RECORD and sch.category is ActionCategory.SCHEDULE
    (cust,) = items_of(plan, A.DELETE_ROWS, "customer")
    assert cust.step_order in sch.depends_on
    c = next(c for c in plan.conflicts if c.code is ConflictCode.EXTERNAL_ARTIFACT)
    assert c.severity is Severity.WARNING and not plan.is_blocked


def test_external_passed_in_artifacts_is_recognised() -> None:
    sh = shop(shared_message=False)
    plan = make_plan(
        sh.schema, sh.db, artifacts=[*sh.artifacts, _external(RefreshStrategy.EXTERNAL_TASK)]
    )
    assert len(items_of(plan, A.SCHEDULE_EXTERNAL)) == 1


def test_external_without_handler_is_unresolved_and_blocking() -> None:
    sh = shop(shared_message=False)
    plan = make_plan(sh.schema, sh.db, external=[_external(RefreshStrategy.NONE)])
    (un,) = items_of(plan, A.UNRESOLVED, "crm_contacts")
    assert "no erasure handler" in un.reason
    assert ConflictCode.OPAQUE_ARTIFACT in codes(plan) and plan.is_blocked


def test_external_keyed_by_non_key_column_is_table_level() -> None:
    sh = shop(shared_message=False)
    plan = make_plan(
        sh.schema, sh.db, external=[_external(RefreshStrategy.EXTERNAL_TASK, ("email",))]
    )
    (sch,) = items_of(plan, A.SCHEDULE_EXTERNAL)
    assert sch.lineage_level is LineageLevel.TABLE and sch.record_keys == ()
    assert sch.expected_row_count is None


def test_external_without_subject_rows_is_ignored() -> None:
    sh = shop(shared_message=False)
    ext = ArtifactDefinition(
        uid("ext2"),
        "erp",
        ArtifactKind.EXTERNAL,
        T("erp"),
        None,
        RefreshStrategy.EXTERNAL_TASK,
        (KeyMapping(T("product"), ("id",), ("product_id",)),),
    )
    plan = make_plan(sh.schema, sh.db, external=[ext])
    assert not items_of(plan, A.SCHEDULE_EXTERNAL)
