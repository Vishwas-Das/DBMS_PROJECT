"""Safe plan execution (architecture.md §11, module_contracts.md §7.2).

The executor runs inside a transaction that the *service* opens (SERIALIZABLE) and later
commits or rolls back. It never commits or rolls back itself. On any problem it raises a
typed error, and the service rolls back everything, so a failed run changes nothing.

Order of work:
1. preconditions: not blocked (PlanBlockedError), approval binds this plan and its hash
   (PlanHashMismatch), structurally valid plan (PlanError);
2. drift check through `rediscover(writer)` BEFORE any write (StalePlanError);
3. ``lock_rows`` for every in-database RECORD item, in plan order, asserting the count;
4. each step in order, asserting the rowcount of every DML step (RowCountMismatch);
5. LIVE only: append ``EXECUTION_COMMITTED`` to the audit sink (same transaction).

DRY_RUN performs exactly the same reads and writes; the service always rolls it back.
Errors and logs carry tables, actions, step numbers and counts only, never key values.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from lae.contracts.enums import (
    AuditEventType,
    ExecutionMode,
    ExecutionStatus,
    LineageLevel,
    PlanAction,
    StepStatus,
)
from lae.contracts.ports import AuditSink, RediscoverFn, TargetWriter
from lae.contracts.types import (
    Approval,
    ColumnAssignment,
    ColumnRef,
    ErasurePlan,
    ExecutionResult,
    PlanItem,
    StepResult,
    TableRef,
)
from lae.errors import (
    ExecutionError,
    LaeError,
    PlanBlockedError,
    PlanError,
    PlanHashMismatch,
    RowCountMismatch,
    StalePlanError,
    ValidationError,
)

__all__ = ["DEFINITION_TOKEN", "execute_plan"]

logger = logging.getLogger(__name__)

DEFINITION_TOKEN = "definition_sha256="  # noqa: S105 - must equal lae.planner.text.DEFINITION_TOKEN

_NULL = ColumnAssignment("NULL")
_PSEUDONYM = ColumnAssignment("PSEUDONYM")
_NO_DML = frozenset({PlanAction.SCHEDULE_EXTERNAL, PlanAction.MANUAL_REVIEW, PlanAction.UNRESOLVED})
_NOT_IN_DB = frozenset({PlanAction.SCHEDULE_EXTERNAL, PlanAction.UNRESOLVED})
_KEY_DELETE = frozenset(
    {PlanAction.DELETE_ROWS, PlanAction.DELETE_DERIVED_BY_KEY, PlanAction.INVALIDATE_CACHE}
)


@dataclass(frozen=True, slots=True)
class _Outcome:
    status: StepStatus
    rows: int | None


def _step_dict(s: StepResult) -> dict[str, Any]:
    return {
        "step_order": s.step_order,
        "status": s.status.value,
        "rows_affected": s.rows_affected,
        "expected_rows": s.expected_rows,
        "duration_ms": s.duration_ms,
    }


def _ms(start: datetime, end: datetime) -> int:
    return max(0, (end - start) // timedelta(milliseconds=1))


def _now(clock: Callable[[], datetime]) -> datetime:
    t = clock()
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValidationError("executor clock must return timezone-aware datetimes")
    return t


def _validate(plan: ErasurePlan) -> None:
    seen: set[int] = set()
    previous = 0
    for item in plan.items:
        if item.step_order <= previous:
            raise PlanError(
                "plan items must have strictly increasing step_order",
                code="INVALID_PLAN",
                details={"step_order": item.step_order},
            )
        previous = item.step_order
        if any(d not in seen for d in item.depends_on):
            raise PlanError(
                "a plan item depends on a step that does not run before it",
                code="INVALID_PLAN",
                details={"step_order": item.step_order},
            )
        if any(len(k) != len(item.key_columns) for k in item.record_keys):
            raise PlanError(
                "record key arity does not match key_columns",
                code="INVALID_PLAN",
                details={"step_order": item.step_order, "table": item.table.qualified()},
            )
        seen.add(item.step_order)


def _locks(item: PlanItem) -> bool:
    return (
        item.lineage_level is LineageLevel.RECORD
        and item.action not in _NOT_IN_DB
        and bool(item.record_keys)
    )


class _Run:
    def __init__(
        self,
        plan: ErasurePlan,
        writer: TargetWriter,
        clock: Callable[[], datetime],
        pseudonym_columns: Collection[ColumnRef],
        recompute_sql: Mapping[TableRef, str],
    ) -> None:
        self.plan = plan
        self.writer = writer
        self.clock = clock
        self.pseudonyms = frozenset(pseudonym_columns)
        self.recompute_sql = recompute_sql
        self.locked: dict[int, int] = {}
        self.done: list[StepResult] = []

    def fail(self, exc: Exception, item: PlanItem | None) -> LaeError:
        """Attach the failed step and the completed step counts; never key values."""
        details: dict[str, Any] = {
            "plan_id": str(self.plan.plan_id),
            "completed_steps": [_step_dict(s) for s in self.done],
        }
        if item is not None:
            details.update(
                step_order=item.step_order, action=item.action.value, table=item.table.qualified()
            )
        if isinstance(exc, LaeError):
            exc.details.update(details)
            return exc
        logger.error(
            "plan %s step %s failed with %s",
            self.plan.plan_id,
            item.step_order if item else None,
            type(exc).__name__,
        )
        where = f"step {item.step_order} ({item.action.value} on {item.table})" if item else "lock"
        return ExecutionError(
            f"{where} failed: {type(exc).__name__}",
            code="STEP_FAILED",
            details={**details, "error_type": type(exc).__name__},
        )

    @staticmethod
    def check(item: PlanItem, actual: int) -> None:
        if item.expected_row_count is not None and actual != item.expected_row_count:
            raise RowCountMismatch(item.step_order, item.expected_row_count, actual)

    def lock_all(self) -> None:
        for item in self.plan.items:
            if not _locks(item):
                continue
            try:
                n = self.writer.lock_rows(item.table, item.key_columns, item.record_keys)
                self.check(item, n)
            except Exception as exc:
                raise self.fail(exc, item) from None
            self.locked[item.step_order] = n

    def run_step(self, item: PlanItem) -> _Outcome:
        w, a = self.writer, item.action
        if a in _NO_DML:
            return _Outcome(StepStatus.SKIPPED, None)
        if a in _KEY_DELETE:
            n = w.delete_rows(item.table, item.key_columns, item.record_keys)
            self.check(item, n)
            return _Outcome(StepStatus.APPLIED, n)
        if a is PlanAction.EXPECT_CASCADE:
            remaining = w.count_by_columns(item.table, item.key_columns, item.record_keys)
            before = self.locked.get(item.step_order, 0)
            removed = before - remaining
            if remaining != 0:
                raise RowCountMismatch(item.step_order, item.expected_row_count or 0, removed)
            self.check(item, removed)
            return _Outcome(StepStatus.APPLIED, removed)
        if a in (PlanAction.ANONYMIZE_ROWS, PlanAction.NULLIFY_REFERENCE):
            assignments = {
                c: (
                    _PSEUDONYM
                    if a is PlanAction.ANONYMIZE_ROWS
                    and ColumnRef(item.table, c) in self.pseudonyms
                    else _NULL
                )
                for c in item.target_columns
            }
            n = w.update_columns(item.table, item.key_columns, item.record_keys, assignments)
            self.check(item, n)
            return _Outcome(StepStatus.APPLIED, n)
        if a is PlanAction.REFRESH_MATVIEW:
            w.refresh_matview(item.table)
            return _Outcome(StepStatus.APPLIED, None)
        if a is PlanAction.RECOMPUTE_TABLE:
            sql = self.recompute_sql.get(item.table)
            if sql is None:
                raise PlanError(
                    "no definition SQL supplied for RECOMPUTE_TABLE",
                    code="MISSING_DEFINITION",
                    details={"table": item.table.qualified()},
                )
            token = DEFINITION_TOKEN + hashlib.sha256(sql.encode("utf-8")).hexdigest()
            if token not in item.reason:
                raise PlanHashMismatch(
                    "the definition SQL differs from the one in the approved plan",
                    details={"table": item.table.qualified()},
                )
            n = w.recompute_table(item.table, sql)
            self.check(item, n)
            return _Outcome(StepStatus.APPLIED, n)
        if a is PlanAction.RETAIN_ROWS:
            n = w.count_by_columns(item.table, item.key_columns, item.record_keys)
            self.check(item, n)
            return _Outcome(StepStatus.APPLIED, n)
        raise PlanError(  # pragma: no cover - every PlanAction is handled above
            "unsupported plan action", code="INVALID_PLAN", details={"action": a.value}
        )

    def run_all(self) -> None:
        for item in self.plan.items:
            start = _now(self.clock)
            try:
                out = self.run_step(item)
            except Exception as exc:
                raise self.fail(exc, item) from None
            result = StepResult(
                step_order=item.step_order,
                status=out.status,
                rows_affected=out.rows,
                expected_rows=item.expected_row_count,
                duration_ms=_ms(start, _now(self.clock)),
            )
            self.done.append(result)
            logger.debug(
                "plan %s step %d %s on %s: %s rows=%s",
                self.plan.plan_id,
                item.step_order,
                item.action.value,
                item.table,
                out.status.value,
                out.rows,
            )


def execute_plan(
    plan: ErasurePlan,
    approval: Approval,
    writer: TargetWriter,
    rediscover: RediscoverFn,
    audit: AuditSink,
    mode: ExecutionMode,
    *,
    clock: Callable[[], datetime],
    pseudonym_columns: Collection[ColumnRef] = (),
    recompute_sql: Mapping[TableRef, str] | None = None,
    plan_hasher: Callable[[ErasurePlan], str] | None = None,
    actor: str = "lae-executor",
) -> ExecutionResult:
    """Execute an approved plan inside the caller's transaction.

    `pseudonym_columns`: ANONYMIZE target columns that get a PSEUDONYM (all others get NULL);
    see `lae.planner.pseudonym_columns`. `recompute_sql`: definition SQL per RECOMPUTE_TABLE
    target; it must match the ``definition_sha256`` bound in the approved item reason.
    `plan_hasher` (e.g. `lae.planner.compute_plan_hash`): if given, the plan content is
    re-hashed and must equal `plan.plan_hash`.

    Returns status COMMITTED for LIVE (all steps applied; the caller commits) and ROLLED_BACK
    for DRY_RUN (the caller must roll back). Raises PlanBlockedError, PlanHashMismatch,
    PlanError, StalePlanError, RowCountMismatch or ExecutionError; it never commits or rolls back.
    """
    if plan.is_blocked:
        raise PlanBlockedError(
            "plan has unresolved blocking conflicts",
            details={
                "plan_id": str(plan.plan_id),
                "unresolved": sorted({c.code.value for c in plan.conflicts if not c.resolved}),
            },
        )
    if approval.plan_id != plan.plan_id or approval.plan_hash != plan.plan_hash:
        raise PlanHashMismatch(
            "approval does not match this plan", details={"plan_id": str(plan.plan_id)}
        )
    if plan_hasher is not None and plan_hasher(plan) != plan.plan_hash:
        raise PlanHashMismatch(
            "plan content does not match its hash", details={"plan_id": str(plan.plan_id)}
        )
    _validate(plan)
    started = _now(clock)
    logger.info("executing plan %s (%s, %d steps)", plan.plan_id, mode.value, len(plan.items))

    fingerprint, record_set_hash = rediscover(writer)
    schema_changed = fingerprint != plan.schema_fingerprint
    records_changed = record_set_hash != plan.record_set_hash
    if schema_changed or records_changed:
        raise StalePlanError(
            "the data or schema changed since the plan was built; re-plan",
            details={
                "plan_id": str(plan.plan_id),
                "schema_changed": schema_changed,
                "records_changed": records_changed,
            },
        )

    run = _Run(plan, writer, clock, pseudonym_columns, recompute_sql or {})
    run.lock_all()
    run.run_all()
    steps = tuple(run.done)
    finished = _now(clock)

    if mode is ExecutionMode.LIVE:
        audit.append(
            AuditEventType.EXECUTION_COMMITTED,
            "erasure_plan",
            plan.plan_id,
            actor,
            {
                "plan_id": plan.plan_id,
                "plan_hash": plan.plan_hash,
                "mode": mode.value,
                "approved_by": approval.approved_by,
                "started_at": started,
                "finished_at": finished,
                "steps": [_step_dict(s) for s in steps],
            },
        )
        status = ExecutionStatus.COMMITTED
    else:
        status = ExecutionStatus.ROLLED_BACK
    logger.info("plan %s %s: %d steps", plan.plan_id, mode.value, len(steps))
    return ExecutionResult(mode=mode, status=status, steps=steps, error=None)
