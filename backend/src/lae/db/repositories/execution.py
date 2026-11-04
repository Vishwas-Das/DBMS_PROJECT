"""ExecutionRepo: execution runs and per-step results."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from lae.contracts.enums import ExecutionMode, ExecutionStatus, StepStatus
from lae.contracts.types import ErrorInfo, StepResult
from lae.db.models import ExecutionRun, ExecutionStepResult, PlanItemModel
from lae.db.records import ExecutionRunRecord
from lae.db.repositories._base import Repo, constraint_of
from lae.errors import AlreadyExecutedError, ValidationError

LIVE_COMMITTED_INDEX = "uq_execution_run_live_committed"


class ExecutionRepo(Repo):
    """Methods:
    start(plan_id, mode, *, isolation_level="SERIALIZABLE", started_at=None) -> ExecutionRunRecord
    finish(run_id, status, *, finished_at, error=None) -> ExecutionRunRecord
        (AlreadyExecutedError if a second LIVE run of the plan would become COMMITTED)
    record_steps(run_id, steps) -> int        (StepResult.step_order -> plan_item of the run's plan)
    get(run_id) -> ExecutionRunRecord         (steps by step_order)
    committed_live(plan_id) -> ExecutionRunRecord | None
    latest_for_plan(plan_id, mode=None) -> ExecutionRunRecord | None
    list_for_plan(plan_id) -> list[ExecutionRunRecord]           (oldest first)
    """

    def start(
        self,
        plan_id: UUID,
        mode: ExecutionMode,
        *,
        isolation_level: str = "SERIALIZABLE",
        started_at: datetime | None = None,
    ) -> ExecutionRunRecord:
        m = ExecutionRun(
            plan_id=plan_id,
            mode=ExecutionMode(mode).value,
            status=ExecutionStatus.RUNNING.value,
            isolation_level=isolation_level,
        )
        if started_at is not None:
            m.started_at = started_at
        self.session.add(m)
        self.session.flush()
        self.session.refresh(m)
        return self._record(m)

    def finish(
        self,
        run_id: UUID,
        status: ExecutionStatus,
        *,
        finished_at: datetime,
        error: ErrorInfo | None = None,
    ) -> ExecutionRunRecord:
        m = self._get(ExecutionRun, run_id)
        try:
            with self.session.begin_nested():
                m.status = ExecutionStatus(status).value
                m.finished_at = finished_at
                m.error = None if error is None else {"code": error.code, "message": error.message}
                self.session.flush()
        except IntegrityError as exc:
            if constraint_of(exc) == LIVE_COMMITTED_INDEX:
                raise AlreadyExecutedError(
                    "plan already has a committed LIVE execution",
                    details={"plan_id": str(m.plan_id)},
                ) from None
            raise
        return self._record(m)

    def record_steps(self, run_id: UUID, steps: Sequence[StepResult]) -> int:
        run = self._get(ExecutionRun, run_id)
        q = select(PlanItemModel.step_order, PlanItemModel.id).where(
            PlanItemModel.plan_id == run.plan_id
        )
        ids = {int(s): i for s, i in self.session.execute(q)}
        for st in steps:
            if st.step_order not in ids:
                raise ValidationError(
                    "step result for an unknown plan step", details={"step_order": st.step_order}
                )
            self.session.add(
                ExecutionStepResult(
                    run_id=run_id,
                    plan_item_id=ids[st.step_order],
                    status=StepStatus(st.status).value,
                    rows_affected=st.rows_affected,
                    expected_rows=st.expected_rows,
                    duration_ms=st.duration_ms,
                )
            )
        self.session.flush()
        return len(steps)

    def _steps(self, run_id: UUID) -> tuple[StepResult, ...]:
        q = (
            select(ExecutionStepResult, PlanItemModel.step_order)
            .join(PlanItemModel, PlanItemModel.id == ExecutionStepResult.plan_item_id)
            .where(ExecutionStepResult.run_id == run_id)
            .order_by(PlanItemModel.step_order)
        )
        return tuple(
            StepResult(
                step_order=int(order),
                status=StepStatus(r.status),
                rows_affected=r.rows_affected,
                expected_rows=r.expected_rows,
                duration_ms=r.duration_ms,
            )
            for r, order in self.session.execute(q)
        )

    def _record(self, m: ExecutionRun) -> ExecutionRunRecord:
        err = m.error
        return ExecutionRunRecord(
            id=m.id,
            plan_id=m.plan_id,
            mode=ExecutionMode(m.mode),
            status=ExecutionStatus(m.status),
            isolation_level=m.isolation_level,
            started_at=m.started_at,
            finished_at=m.finished_at,
            error=None if err is None else ErrorInfo(str(err["code"]), str(err["message"])),
            steps=self._steps(m.id),
        )

    def get(self, run_id: UUID) -> ExecutionRunRecord:
        return self._record(self._get(ExecutionRun, run_id))

    def committed_live(self, plan_id: UUID) -> ExecutionRunRecord | None:
        q = select(ExecutionRun).where(
            ExecutionRun.plan_id == plan_id,
            ExecutionRun.mode == ExecutionMode.LIVE.value,
            ExecutionRun.status == ExecutionStatus.COMMITTED.value,
        )
        m = self.session.scalars(q).first()
        return None if m is None else self._record(m)

    def latest_for_plan(
        self, plan_id: UUID, mode: ExecutionMode | None = None
    ) -> ExecutionRunRecord | None:
        q = select(ExecutionRun).where(ExecutionRun.plan_id == plan_id)
        if mode is not None:
            q = q.where(ExecutionRun.mode == ExecutionMode(mode).value)
        q = q.order_by(ExecutionRun.started_at.desc(), ExecutionRun.id.desc()).limit(1)
        m = self.session.scalars(q).first()
        return None if m is None else self._record(m)

    def list_for_plan(self, plan_id: UUID) -> list[ExecutionRunRecord]:
        q = (
            select(ExecutionRun)
            .where(ExecutionRun.plan_id == plan_id)
            .order_by(ExecutionRun.started_at, ExecutionRun.id)
        )
        return [self._record(m) for m in self.session.scalars(q)]
