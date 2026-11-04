"""Executor behaviour on the in-memory FakeDB (FK semantics, statement atomicity).

The service-owned transaction is simulated with `run_in_txn` (rollback on error and for
DRY_RUN). The executor itself never commits or rolls back.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from erasure_harness import (
    SEGMENT_SQL,
    FakeAudit,
    FakeDB,
    Schema,
    StepClock,
    T,
    ack_all,
    items_of,
    make_plan,
    marketing_counts,
    pseudonym,
    rediscover_fn,
    run_in_txn,
    shop,
)

from lae.contracts.enums import (
    ArtifactKind,
    AuditEventType,
    ExecutionMode,
    ExecutionStatus,
    OnDeleteAction,
    PlanAction,
    RefreshStrategy,
    RetentionAction,
    StepStatus,
    Traceability,
)
from lae.contracts.ports import TargetWriter
from lae.contracts.types import (
    Approval,
    ArtifactDefinition,
    ColumnRef,
    ErasurePlan,
    KeyMapping,
    KeyTuple,
    TableRef,
)
from lae.errors import (
    ExecutionError,
    PlanBlockedError,
    PlanError,
    PlanHashMismatch,
    RowCountMismatch,
    StalePlanError,
    TargetQueryError,
    ValidationError,
)
from lae.executor import DEFINITION_TOKEN, execute_plan
from lae.planner import DEFINITION_TOKEN as PLANNER_TOKEN
from lae.planner import compute_plan_hash, plan_with_hash, pseudonym_columns

pytestmark = pytest.mark.unit

LIVE, DRY = ExecutionMode.LIVE, ExecutionMode.DRY_RUN
RAW = ("alice", "Alice", "example.com", "1001", "1002", "(42,)", "[42]")


def approve(plan: ErasurePlan, plan_hash: str | None = None) -> Approval:
    return Approval(
        plan.plan_id, plan_hash or plan.plan_hash, "reviewer", datetime(2026, 11, 4, tzinfo=UTC)
    )


def ready_plan(sh: Any, **kw: Any) -> ErasurePlan:
    return ack_all(make_plan(sh.schema, sh.db, predicate_row_counts=marketing_counts(sh.db), **kw))


def run(
    sh: Any,
    plan: ErasurePlan,
    mode: ExecutionMode = LIVE,
    *,
    db: FakeDB | None = None,
    approval: Approval | None = None,
    **kw: Any,
) -> tuple[Any, FakeAudit]:
    db = db or sh.db
    audit = FakeAudit()
    kw.setdefault("recompute_sql", {T("customer_segment"): SEGMENT_SQL})
    kw.setdefault("pseudonym_columns", pseudonym_columns(plan, sh.schema.policies(), sh.lineage()))
    kw.setdefault("rediscover", sh.rediscover())
    rediscover = kw.pop("rediscover")
    result = run_in_txn(
        db,
        mode,
        lambda: execute_plan(
            plan, approval or approve(plan), db, rediscover, audit, mode, clock=StepClock(), **kw
        ),
    )
    return result, audit


def by_id(db: FakeDB, table: str, col: str = "id") -> dict[Any, dict[str, Any]]:
    return {r[col]: dict(r) for r in db.rows(table)}


def assert_no_raw(err: Exception) -> None:
    text = f"{err} {getattr(err, 'details', '')} {err!r}"
    for r in RAW:
        assert r not in text, (r, text)


# ---------------------------------------------------------------------- happy path


def test_live_happy_path_erases_subject_and_keeps_others() -> None:
    sh = shop()
    before = sh.db.snapshot()
    plan = ready_plan(sh)
    result, audit = run(sh, plan)
    db = sh.db
    assert (
        result.mode is LIVE and result.status is ExecutionStatus.COMMITTED and result.error is None
    )
    assert [s.step_order for s in result.steps] == [i.step_order for i in plan.items]
    for item, step in zip(plan.items, result.steps, strict=True):
        assert step.expected_rows == item.expected_row_count
        assert step.duration_ms == 5
        if item.action is PlanAction.MANUAL_REVIEW:
            assert step.status is StepStatus.SKIPPED and step.rows_affected is None
        else:
            assert step.status is StepStatus.APPLIED
            if item.expected_row_count is not None:
                assert step.rows_affected == item.expected_row_count
    # subject data gone
    assert set(by_id(db, "customer")) == {77, 99}
    assert set(by_id(db, "customer_address")) == {3}
    assert set(by_id(db, "purchase_order")) == {2001}
    assert {(r["order_id"], r["line_no"]) for r in db.rows("order_line")} == {(2001, 1)}
    assert set(by_id(db, "shipment")) == {502} and set(by_id(db, "payment")) == {602}
    assert set(by_id(db, "support_ticket")) == {302}
    assert set(by_id(db, "ticket_message")) == {403}  # 402 (acknowledged shared) cascaded
    assert [r["customer_id"] for r in db.rows("marketing_event_log")] == [99]
    assert set(by_id(db, "document_embedding")) == {802}
    assert set(by_id(db, "customer_profile_cache", "customer_id")) == {99}
    assert set(by_id(db, "customer_ltv", "customer_id")) == {77, 99}
    assert set(by_id(db, "customer_segment", "customer_id")) == {77, 99}
    assert db.rows("daily_revenue")[0]["total"] == 20
    # other subjects untouched (77 only loses its reference to the subject)
    old = {r["id"]: r for r in before[T("customer")]}
    assert by_id(db, "customer")[99] == old[99]
    assert by_id(db, "customer")[77] == {**old[77], "referrer_id": None}
    assert by_id(db, "product") == {7: {"id": 7, "title": "widget"}}
    # one audit event, in the same transaction, without personal data
    (event,) = audit.events
    etype, entity, entity_id, actor, payload = event
    assert etype is AuditEventType.EXECUTION_COMMITTED and entity == "erasure_plan"
    assert entity_id == plan.plan_id and actor == "lae-executor"
    assert payload["plan_hash"] == plan.plan_hash and len(payload["steps"]) == len(plan.items)
    assert "alice" not in str(payload) and "1001" not in str(payload)


def test_rediscovery_after_live_run_finds_only_the_cleanup_state() -> None:
    sh = shop(shared_message=False)
    plan = ready_plan(sh)
    run(sh, plan)
    assert 42 not in by_id(sh.db, "customer")


def test_dry_run_performs_identical_operations_and_changes_nothing() -> None:
    a, b = shop(), shop()
    checksum = a.db.checksum()
    plan = ready_plan(a)
    assert plan == ready_plan(b)
    a.db.ops.clear()
    b.db.ops.clear()
    dry, dry_audit = run(a, plan, DRY)
    live, _ = run(b, plan, LIVE)
    assert a.db.ops == b.db.ops
    assert dry.steps == live.steps
    assert dry.status is ExecutionStatus.ROLLED_BACK and dry.mode is DRY
    assert dry_audit.events == []
    assert a.db.checksum() == checksum
    assert b.db.checksum() != checksum


def test_anonymize_writes_pseudonyms_and_nulls() -> None:
    sh = shop(shared_message=False)
    sh.schema.policy("purchase_order", RetentionAction.ANONYMIZE)
    plan = ready_plan(sh)
    run(sh, plan)
    po = by_id(sh.db, "purchase_order")
    assert set(po) == {1001, 1002, 2001}
    for o in (1001, 1002):
        assert po[o]["customer_id"] is None
        assert po[o]["shipping_name"] == pseudonym(T("purchase_order"), (o,), "shipping_name")
    assert po[2001]["shipping_name"] == "name 99"
    assert not [r for r in sh.db.rows("order_line") if r["order_id"] in (1001, 1002)]


def test_retain_rows_are_counted_not_modified() -> None:
    sh = shop(shared_message=False)
    sh.schema.policy("purchase_order", RetentionAction.RETAIN)
    plan = ready_plan(sh)
    result, _ = run(sh, plan)
    (ret,) = items_of(plan, PlanAction.RETAIN_ROWS, "purchase_order")
    step = result.steps[ret.step_order - 1]
    assert step.status is StepStatus.APPLIED and step.rows_affected == 2
    po = by_id(sh.db, "purchase_order")
    assert po[1001]["shipping_name"] == "name 42" and po[1001]["customer_id"] is None


def test_breakable_cycle_executes() -> None:
    s = Schema()
    s.table("customer", "id")
    s.table("node", "id", "customer_id", "peer_id", not_null=("customer_id",))
    s.table("other", "id", "node_id", not_null=("node_id",))
    s.fk("fk_node_customer", "node", ["customer_id"], "customer", ["id"], OnDeleteAction.NO_ACTION)
    s.fk("fk_other_node", "other", ["node_id"], "node", ["id"], OnDeleteAction.NO_ACTION)
    s.fk("fk_node_peer", "node", ["peer_id"], "other", ["id"], OnDeleteAction.NO_ACTION)
    db = FakeDB(s)
    db.insert("customer", id=1)
    db.insert("customer", id=2)
    db.insert("node", id=10, customer_id=1, peer_id=20)
    db.insert("other", id=20, node_id=10)
    plan = make_plan(s, db, subject=(1,))
    audit = FakeAudit()
    res = execute_plan(
        plan, approve(plan), db, rediscover_fn(s, "customer", (1,)), audit, LIVE, clock=StepClock()
    )
    assert res.status is ExecutionStatus.COMMITTED
    assert db.ids("customer") == [2] and db.rows("node") == [] and db.rows("other") == []


# ---------------------------------------------------------------------- preconditions


def test_blocked_plan_is_rejected_before_any_access() -> None:
    sh = shop()
    plan = make_plan(sh.schema, sh.db)
    assert plan.is_blocked
    sh.db.ops.clear()
    with pytest.raises(PlanBlockedError) as ei:
        run(sh, plan)
    assert ei.value.details["unresolved"] == ["NO_PRIMARY_KEY", "SHARED_RECORD"]
    assert sh.db.ops == []


def test_hash_mismatch_and_wrong_plan_are_rejected() -> None:
    sh = shop()
    plan = ready_plan(sh)
    sh.db.ops.clear()
    with pytest.raises(PlanHashMismatch):
        run(sh, plan, approval=approve(plan, "0" * 64))
    with pytest.raises(PlanHashMismatch):
        run(sh, plan, approval=replace(approve(plan), plan_id=sh.ltv.artifact_id))
    tampered = replace(plan, items=plan.items[:-1])  # stale stored hash
    with pytest.raises(PlanHashMismatch):
        run(sh, tampered, plan_hasher=compute_plan_hash)
    assert sh.db.ops == []


def test_plan_hasher_accepts_untampered_plan() -> None:
    sh = shop(shared_message=False)
    plan = ready_plan(sh)
    result, _ = run(sh, plan, plan_hasher=compute_plan_hash)
    assert result.status is ExecutionStatus.COMMITTED


@pytest.mark.parametrize(
    "mutate",
    [
        lambda items: (items[1], items[0], *items[2:]),
        lambda items: (replace(items[0], depends_on=(items[1].step_order,)), *items[1:]),
        lambda items: (replace(items[0], record_keys=((1, 2),)), *items[1:]),
    ],
    ids=["order", "forward-dependency", "key-arity"],
)
def test_structurally_invalid_plans_are_rejected(mutate: Any) -> None:
    sh = shop(shared_message=False)
    plan = ready_plan(sh)
    bad = plan_with_hash(replace(plan, items=mutate(plan.items)))
    with pytest.raises(PlanError) as ei:
        run(sh, bad)
    assert ei.value.code == "INVALID_PLAN"


def test_naive_clock_is_rejected() -> None:
    sh = shop(shared_message=False)
    plan = ready_plan(sh)
    with pytest.raises(ValidationError):
        execute_plan(
            plan,
            approve(plan),
            sh.db,
            sh.rediscover(),
            FakeAudit(),
            LIVE,
            clock=lambda: datetime(2026, 1, 1),
        )


# ---------------------------------------------------------------------- drift


def test_new_order_after_planning_is_stale_and_nothing_is_written() -> None:
    sh = shop()
    plan = ready_plan(sh)
    sh.db.insert("purchase_order", id=1003, customer_id=42, shipping_name="late")
    checksum = sh.db.checksum()
    sh.db.ops.clear()
    with pytest.raises(StalePlanError) as ei:
        run(sh, plan)
    assert ei.value.details["records_changed"] is True
    assert ei.value.details["schema_changed"] is False
    assert {op[0] for op in sh.db.ops} <= {"select"}  # rediscovery only reads
    assert sh.db.checksum() == checksum
    assert_no_raw(ei.value)


def test_schema_change_is_stale() -> None:
    sh = shop(shared_message=False)
    plan = ready_plan(sh)
    real = sh.rediscover()
    with pytest.raises(StalePlanError) as ei:
        run(sh, plan, rediscover=lambda r: ("f" * 64, real(r)[1]))
    assert ei.value.details["schema_changed"] is True


# ---------------------------------------------------------------------- rowcount and failures


def test_missing_predicate_count_fails_at_lock_with_counts_only() -> None:
    sh = shop(shared_message=False)
    plan = ack_all(make_plan(sh.schema, sh.db))  # no predicate counts: expects 1, finds 2
    checksum = sh.db.checksum()
    with pytest.raises(RowCountMismatch) as ei:
        run(sh, plan)
    (mk,) = items_of(plan, PlanAction.DELETE_ROWS, "marketing_event_log")
    e = ei.value
    assert (e.step_order, e.expected, e.actual) == (mk.step_order, 1, 2)
    assert e.details["completed_steps"] == [] and e.details["table"] == "app.marketing_event_log"
    assert sh.db.checksum() == checksum
    assert_no_raw(e)


class MiscountingDB(FakeDB):
    """Returns a wrong rowcount for deletes on one table (e.g. a concurrent writer)."""

    def __init__(self, base: FakeDB, table: TableRef) -> None:
        super().__init__(base.schema)
        self.data, self.builders, self.bad = base.data, base.builders, table

    def delete_rows(self, table: TableRef, key_columns: Any, keys: Any) -> int:
        n = super().delete_rows(table, key_columns, keys)
        return n + 1 if table == self.bad else n


def test_dml_rowcount_mismatch_rolls_back_everything() -> None:
    sh = shop()
    plan = ready_plan(sh)
    db = MiscountingDB(sh.db, T("purchase_order"))
    checksum = db.checksum()
    with pytest.raises(RowCountMismatch) as ei:
        run(sh, plan, db=db)
    (po,) = items_of(plan, PlanAction.DELETE_ROWS, "purchase_order")
    assert ei.value.step_order == po.step_order
    assert ei.value.expected == 2 and ei.value.actual == 3
    done = ei.value.details["completed_steps"]
    assert [d["step_order"] for d in done] == list(range(1, po.step_order))
    assert all(
        set(d) == {"step_order", "status", "rows_affected", "expected_rows", "duration_ms"}
        for d in done
    )
    assert db.checksum() == checksum
    assert_no_raw(ei.value)


class SurvivingCascadeDB(FakeDB):
    def __init__(self, base: FakeDB, table: TableRef) -> None:
        super().__init__(base.schema)
        self.data, self.builders, self.bad = base.data, base.builders, table

    def count_by_columns(self, table: TableRef, columns: Any, values: Any) -> int:
        n = super().count_by_columns(table, columns, values)
        return n + 1 if table == self.bad else n


def test_cascade_that_leaves_rows_is_a_mismatch() -> None:
    sh = shop()
    plan = ready_plan(sh)
    db = SurvivingCascadeDB(sh.db, T("customer_address"))
    with pytest.raises(RowCountMismatch) as ei:
        run(sh, plan, db=db)
    (ex,) = items_of(plan, PlanAction.EXPECT_CASCADE, "customer_address")
    assert ei.value.step_order == ex.step_order and ei.value.actual == 1


def test_retain_count_mismatch() -> None:
    sh = shop(shared_message=False)
    sh.schema.policy("purchase_order", RetentionAction.RETAIN)
    plan = ready_plan(sh)
    db = SurvivingCascadeDB(sh.db, T("purchase_order"))
    with pytest.raises(RowCountMismatch) as ei:
        run(sh, plan, db=db)
    (ret,) = items_of(plan, PlanAction.RETAIN_ROWS)
    assert ei.value.step_order == ret.step_order


def _dml_steps(plan: ErasurePlan) -> list[int]:
    no_dml = {PlanAction.MANUAL_REVIEW, PlanAction.SCHEDULE_EXTERNAL, PlanAction.UNRESOLVED}
    return [i.step_order for i in plan.items if i.action not in no_dml]


OP_OF = {
    PlanAction.DELETE_ROWS: "delete",
    PlanAction.DELETE_DERIVED_BY_KEY: "delete",
    PlanAction.INVALIDATE_CACHE: "delete",
    PlanAction.NULLIFY_REFERENCE: "update",
    PlanAction.ANONYMIZE_ROWS: "update",
    PlanAction.EXPECT_CASCADE: "count",
    PlanAction.RETAIN_ROWS: "count",
    PlanAction.REFRESH_MATVIEW: "refresh",
    PlanAction.RECOMPUTE_TABLE: "recompute",
}


@pytest.mark.parametrize("k", range(15))
def test_injected_failure_at_any_step_rolls_back_fully(k: int) -> None:
    """AT-15 on the fake: a failure at step k leaves every table unchanged."""
    sh = shop()
    plan = ready_plan(sh)
    steps = _dml_steps(plan)
    if k >= len(steps):
        pytest.skip("plan has fewer DML steps")
    chosen = plan.items[steps[k] - 1]
    op = OP_OF[chosen.action]
    # the first step performing this operation on this table is the one that fails
    item = next(i for i in plan.items if OP_OF.get(i.action) == op and i.table == chosen.table)
    checksum = sh.db.checksum()
    sh.db.fail_on = lambda o, table: o == op and table == item.table
    with pytest.raises(ExecutionError) as ei:
        run(sh, plan)
    assert ei.value.code == "STEP_FAILED"
    assert ei.value.details["step_order"] == item.step_order
    assert ei.value.details["action"] == item.action.value
    assert ei.value.details["error_type"] == "RuntimeError"
    assert len(ei.value.details["completed_steps"]) == item.step_order - 1
    assert sh.db.checksum() == checksum
    assert_no_raw(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__


def test_failure_during_locking_is_wrapped() -> None:
    sh = shop(shared_message=False)
    plan = ready_plan(sh)
    sh.db.fail_on = lambda op, table: op == "lock"
    with pytest.raises(ExecutionError) as ei:
        run(sh, plan)
    assert ei.value.details["step_order"] == plan.items[0].step_order
    assert_no_raw(ei.value)


def test_typed_writer_error_keeps_its_type_and_gains_details() -> None:
    sh = shop(shared_message=False)
    plan = ready_plan(sh)
    # remove the nullify of customer 77's referrer: deleting 42 then violates nothing (SET NULL),
    # so instead make payment undeletable by adding a hidden child row the plan does not know
    sh.schema.table("refund", "id", "payment_id", not_null=("payment_id",))
    sh.schema.fk(
        "fk_refund_payment", "refund", ["payment_id"], "payment", ["id"], OnDeleteAction.NO_ACTION
    )
    sh.db.data[T("refund")] = [{"id": 1, "payment_id": 601}]
    with pytest.raises(TargetQueryError) as ei:
        run(sh, plan, rediscover=lambda r: (plan.schema_fingerprint, plan.record_set_hash))
    (pay,) = items_of(plan, PlanAction.DELETE_ROWS, "payment")
    assert ei.value.details["step_order"] == pay.step_order
    assert ei.value.details["constraint"] == "fk_refund_payment"


# ---------------------------------------------------------------------- recompute binding


def test_recompute_requires_matching_definition() -> None:
    sh = shop(shared_message=False)
    plan = ready_plan(sh)
    with pytest.raises(PlanError) as ei:
        run(sh, plan, recompute_sql={})
    assert ei.value.code == "MISSING_DEFINITION"
    with pytest.raises(PlanHashMismatch):
        run(sh, plan, recompute_sql={T("customer_segment"): SEGMENT_SQL + " WHERE true"})
    assert DEFINITION_TOKEN == PLANNER_TOKEN


def test_recompute_rowcount_is_reported() -> None:
    sh = shop(shared_message=False)
    plan = ready_plan(sh)
    sh.db.recompute_counts[T("customer_segment")] = 2
    result, _ = run(sh, plan)
    (rc,) = items_of(plan, PlanAction.RECOMPUTE_TABLE)
    assert result.steps[rc.step_order - 1].rows_affected == 2


# ---------------------------------------------------------------------- non-DB steps


def test_external_and_unresolved_steps_are_skipped_and_never_locked() -> None:
    sh = shop(shared_message=False)
    ext_ok = ArtifactDefinition(
        sh.ltv.artifact_id.__class__(int=11),
        "crm",
        ArtifactKind.EXTERNAL,
        T("crm"),
        None,
        RefreshStrategy.EXTERNAL_TASK,
        (KeyMapping(T("customer"), ("id",), ("contact_id",)),),
    )
    ext_bad = replace(
        ext_ok,
        artifact_id=sh.ltv.artifact_id.__class__(int=12),
        name="erp",
        target=T("erp"),
        refresh_strategy=RefreshStrategy.NONE,
    )
    plan = ready_plan(sh, external=[ext_ok, ext_bad])
    assert not plan.is_blocked  # OPAQUE_ARTIFACT acknowledged
    sh.db.ops.clear()
    result, _ = run(sh, plan)
    for action in (PlanAction.SCHEDULE_EXTERNAL, PlanAction.UNRESOLVED):
        (item,) = items_of(plan, action)
        assert result.steps[item.step_order - 1].status is StepStatus.SKIPPED
    assert not [op for op in sh.db.ops if op[1] in (T("crm"), T("erp"))]


def test_opaque_artifact_acknowledged_executes_with_unresolved_skip() -> None:
    sh = shop(shared_message=False)
    plan = ready_plan(sh, affected={T("daily_revenue"): Traceability.OPAQUE})
    result, _ = run(sh, plan)
    (un,) = items_of(plan, PlanAction.UNRESOLVED)
    assert result.steps[un.step_order - 1].status is StepStatus.SKIPPED


def test_fake_db_satisfies_target_writer_protocol() -> None:
    sh = shop()
    assert isinstance(sh.db, TargetWriter)
    assert pseudonym(T("x"), (1,), "c").startswith("anon_")
    assert isinstance(ColumnRef(T("x"), "c"), ColumnRef)
    key: KeyTuple = (1,)
    assert key == (1,)
