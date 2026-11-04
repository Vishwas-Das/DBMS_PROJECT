"""VerificationRepo: verification runs and their checks."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from lae.contracts.enums import CheckType, VerificationOutcome, VerificationStatus
from lae.contracts.types import CheckResult, TableRef, VerificationReport
from lae.db.models import ExecutionRun, PlanItemModel, VerificationCheck, VerificationRun
from lae.db.records import VerificationRunRecord
from lae.db.repositories._base import Repo, constraint_of
from lae.errors import InvalidStateTransition, ValidationError

REQUIRES_COMMITTED_TRIGGER = "trg_verification_run_requires_committed"


class VerificationRepo(Repo):
    """Methods:
    start(execution_run_id, *, started_at=None) -> VerificationRunRecord
        (InvalidStateTransition unless the execution run is LIVE + COMMITTED)
    complete(run_id, report, *, finished_at) -> VerificationRunRecord
        (stores status, outcome, coverage counts and every CheckResult)
    mark_error(run_id, *, finished_at) -> VerificationRunRecord
    get(run_id) -> VerificationRunRecord
    latest_for_execution(execution_run_id) -> VerificationRunRecord | None
    """

    def start(
        self, execution_run_id: UUID, *, started_at: datetime | None = None
    ) -> VerificationRunRecord:
        m = VerificationRun(
            execution_run_id=execution_run_id, status=VerificationStatus.RUNNING.value
        )
        if started_at is not None:
            m.started_at = started_at
        try:
            with self.session.begin_nested():
                self.session.add(m)
                self.session.flush()
        except IntegrityError as exc:
            if constraint_of(exc) == REQUIRES_COMMITTED_TRIGGER:
                raise InvalidStateTransition(
                    "verification requires a LIVE, COMMITTED execution run",
                    details={"execution_run_id": str(execution_run_id)},
                ) from None
            raise
        self.session.refresh(m)
        return self._record(m)

    def _plan_item_ids(self, run: VerificationRun) -> dict[int, UUID]:
        ex = self._get(ExecutionRun, run.execution_run_id)
        q = select(PlanItemModel.step_order, PlanItemModel.id).where(
            PlanItemModel.plan_id == ex.plan_id
        )
        return {int(s): i for s, i in self.session.execute(q)}

    def complete(
        self, run_id: UUID, report: VerificationReport, *, finished_at: datetime
    ) -> VerificationRunRecord:
        m = self._get(VerificationRun, run_id)
        ids = self._plan_item_ids(m)
        for c in report.checks:
            if c.step_order is not None and c.step_order not in ids:
                raise ValidationError(
                    "check bound to an unknown plan step", details={"step_order": c.step_order}
                )
            self.session.add(
                VerificationCheck(
                    verification_run_id=run_id,
                    check_type=CheckType(c.check_type).value,
                    schema_name=None if c.table is None else c.table.schema,
                    table_name=None if c.table is None else c.table.name,
                    column_name=c.column,
                    plan_item_id=None if c.step_order is None else ids[c.step_order],
                    passed=c.passed,
                    observed=dict(c.observed),
                    expected=dict(c.expected),
                )
            )
        m.status = VerificationStatus(report.status).value
        m.outcome = VerificationOutcome(report.outcome).value
        m.required_artifacts = report.required_artifacts
        m.verified_artifacts = report.verified_artifacts
        m.finished_at = finished_at
        self.session.flush()
        return self._record(m)

    def mark_error(self, run_id: UUID, *, finished_at: datetime) -> VerificationRunRecord:
        m = self._get(VerificationRun, run_id)
        m.status = VerificationStatus.ERROR.value
        m.finished_at = finished_at
        self.session.flush()
        return self._record(m)

    def _checks(self, run_id: UUID) -> tuple[CheckResult, ...]:
        q = (
            select(VerificationCheck, PlanItemModel.step_order)
            .outerjoin(PlanItemModel, PlanItemModel.id == VerificationCheck.plan_item_id)
            .where(VerificationCheck.verification_run_id == run_id)
        )
        out: list[tuple[Any, CheckResult]] = []
        for c, step in self.session.execute(q):
            table = (
                None
                if c.schema_name is None or c.table_name is None
                else TableRef(c.schema_name, c.table_name)
            )
            res = CheckResult(
                check_type=CheckType(c.check_type),
                table=table,
                column=c.column_name,
                step_order=None if step is None else int(step),
                passed=c.passed,
                observed=dict(c.observed),
                expected=dict(c.expected),
            )
            key = (
                c.check_type,
                c.schema_name or "",
                c.table_name or "",
                c.column_name or "",
                -1 if step is None else int(step),
                str(c.id),
            )
            out.append((key, res))
        out.sort(key=lambda kv: kv[0])
        return tuple(r for _, r in out)

    def _record(self, m: VerificationRun) -> VerificationRunRecord:
        status = VerificationStatus(m.status)
        outcome = None if m.outcome is None else VerificationOutcome(m.outcome)
        report = VerificationReport(
            status=status,
            checks=self._checks(m.id),
            outcome=outcome or VerificationOutcome.FAILED,
            required_artifacts=m.required_artifacts or 0,
            verified_artifacts=m.verified_artifacts or 0,
        )
        return VerificationRunRecord(
            id=m.id,
            execution_run_id=m.execution_run_id,
            status=status,
            outcome=outcome,
            required_artifacts=m.required_artifacts,
            verified_artifacts=m.verified_artifacts,
            started_at=m.started_at,
            finished_at=m.finished_at,
            report=report,
        )

    def get(self, run_id: UUID) -> VerificationRunRecord:
        return self._record(self._get(VerificationRun, run_id))

    def latest_for_execution(self, execution_run_id: UUID) -> VerificationRunRecord | None:
        q = (
            select(VerificationRun)
            .where(VerificationRun.execution_run_id == execution_run_id)
            .order_by(VerificationRun.started_at.desc(), VerificationRun.id.desc())
            .limit(1)
        )
        m = self.session.scalars(q).first()
        return None if m is None else self._record(m)
