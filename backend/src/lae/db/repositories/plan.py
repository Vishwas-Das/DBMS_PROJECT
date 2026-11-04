"""PlanRepo: erasure plans, plan items, conflicts and the persisted record graph."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update

from lae.contracts.enums import (
    ConflictCode,
    LineageLevel,
    PlanAction,
    PlanStatus,
    RecordRole,
    Severity,
)
from lae.contracts.types import (
    Approval,
    ErasurePlan,
    PlanConflict,
    PlanItem,
    TableRef,
)
from lae.db.models import (
    ErasurePlanModel,
    PlanConflictModel,
    PlanItemModel,
    RecordDependency,
)
from lae.db.models.workflow import LIVE_PLAN_STATUSES
from lae.db.records import ConflictRecord, PlanItemRecord, PlanRecord, RecordDependencyRow
from lae.db.repositories._base import Repo, key_from_json, key_to_json, keys_from_json
from lae.errors import PlanHashMismatch, ValidationError

CONFLICT_RESOLUTIONS = frozenset({"ACKNOWLEDGED", "POLICY_CHANGED"})


def _plan_record(m: ErasurePlanModel) -> PlanRecord:
    return PlanRecord(
        id=m.id,
        request_id=m.request_id,
        snapshot_id=m.snapshot_id,
        version=m.version,
        status=PlanStatus(m.status),
        schema_fingerprint=m.schema_fingerprint,
        record_set_hash=m.record_set_hash,
        plan_hash=m.plan_hash,
        record_count=m.record_count,
        created_at=m.created_at,
        approved_by=m.approved_by,
        approved_at=m.approved_at,
        approved_hash=m.approved_hash,
    )


def _item(m: PlanItemModel) -> PlanItem:
    return PlanItem(
        step_order=m.step_order,
        action=PlanAction(m.action),
        table=TableRef(m.schema_name, m.table_name),
        lineage_level=LineageLevel(m.lineage_level),
        key_columns=tuple(m.key_columns),
        record_keys=keys_from_json(m.record_keys),
        target_columns=tuple(m.target_columns),
        expected_row_count=m.expected_row_count,
        depends_on=tuple(m.depends_on_steps),
        reason_edge_ids=tuple(m.reason_edge_ids),
        reason=m.reason,
    )


def _conflict(m: PlanConflictModel) -> PlanConflict:
    return PlanConflict(
        code=ConflictCode(m.code),
        severity=Severity(m.severity),
        table=TableRef(m.schema_name, m.table_name),
        message=m.message,
        resolved=m.resolution is not None,
    )


def _conflict_key(m: PlanConflictModel) -> tuple[str, str, str, str, str]:
    return (m.schema_name, m.table_name, m.code, m.severity, m.message)


class PlanRepo(Repo):
    """Methods:
    next_version(request_id) -> int
    create(plan, *, snapshot_id, version, record_count, status=None, record_key_hmacs=None)
        -> PlanRecord     (status defaults to BLOCKED if plan.is_blocked else DRAFT)
    get(plan_id) -> ErasurePlan                      (items by step_order, conflicts sorted)
    record(plan_id) -> PlanRecord
    live_for_request(request_id) -> PlanRecord | None    (DRAFT / BLOCKED / APPROVED)
    latest_for_request(request_id) -> PlanRecord | None
    list_for_request(request_id) -> list[PlanRecord]     (by version)
    items(plan_id) -> list[PlanItemRecord]
    item_ids(plan_id) -> dict[int, UUID]                 (step_order -> plan_item.id)
    conflicts(plan_id) -> list[ConflictRecord]
    set_status(plan_id, status) -> PlanRecord
    approve(plan_id, approved_by, approved_at, approved_hash) -> PlanRecord   (PlanHashMismatch)
    approval(plan_id) -> Approval | None
    resolve_conflict(conflict_id, resolution, resolved_by) -> ConflictRecord
    set_plan_hash(plan_id, plan_hash) -> PlanRecord
    save_record_dependencies(plan_id, rows) -> int
    record_dependencies(plan_id) -> list[RecordDependencyRow]
    purge_raw_keys(plan_id) -> int     (plan_item.record_keys and record_dependency raw keys)
    """

    def next_version(self, request_id: UUID) -> int:
        q = select(func.coalesce(func.max(ErasurePlanModel.version), 0)).where(
            ErasurePlanModel.request_id == request_id
        )
        return int(self.session.execute(q).scalar_one()) + 1

    def create(
        self,
        plan: ErasurePlan,
        *,
        snapshot_id: UUID,
        version: int,
        record_count: int,
        status: PlanStatus | None = None,
        record_key_hmacs: Mapping[int, Sequence[str]] | None = None,
    ) -> PlanRecord:
        if status is None:
            status = PlanStatus.BLOCKED if plan.is_blocked else PlanStatus.DRAFT
        hmacs = record_key_hmacs or {}
        m = ErasurePlanModel(
            id=plan.plan_id,
            request_id=plan.request_id,
            snapshot_id=snapshot_id,
            version=version,
            status=PlanStatus(status).value,
            schema_fingerprint=plan.schema_fingerprint,
            record_set_hash=plan.record_set_hash,
            plan_hash=plan.plan_hash,
            record_count=record_count,
        )
        m.items = [
            PlanItemModel(
                step_order=i.step_order,
                action=PlanAction(i.action).value,
                category=i.category.value,
                reason=i.reason,
                schema_name=i.table.schema,
                table_name=i.table.name,
                lineage_level=LineageLevel(i.lineage_level).value,
                key_columns=list(i.key_columns),
                record_keys=[key_to_json(k) for k in i.record_keys] if i.record_keys else None,
                record_key_hmacs=list(hmacs.get(i.step_order, ())),
                target_columns=list(i.target_columns),
                expected_row_count=i.expected_row_count,
                depends_on_steps=list(i.depends_on),
                reason_edge_ids=list(i.reason_edge_ids),
            )
            for i in plan.items
        ]
        m.conflicts = [
            PlanConflictModel(
                code=ConflictCode(c.code).value,
                severity=Severity(c.severity).value,
                schema_name=c.table.schema,
                table_name=c.table.name,
                message=c.message,
                resolution="ACKNOWLEDGED" if c.resolved else None,
            )
            for c in plan.conflicts
        ]
        self.session.add(m)
        self.session.flush()
        self.session.refresh(m)
        return _plan_record(m)

    def _plan(self, plan_id: UUID) -> ErasurePlanModel:
        return self._get(ErasurePlanModel, plan_id)

    def get(self, plan_id: UUID) -> ErasurePlan:
        m = self._plan(plan_id)
        return ErasurePlan(
            plan_id=m.id,
            request_id=m.request_id,
            schema_fingerprint=m.schema_fingerprint,
            record_set_hash=m.record_set_hash,
            items=tuple(_item(i) for i in sorted(m.items, key=lambda i: i.step_order)),
            conflicts=tuple(_conflict(c) for c in sorted(m.conflicts, key=_conflict_key)),
            plan_hash=m.plan_hash,
        )

    def record(self, plan_id: UUID) -> PlanRecord:
        return _plan_record(self._plan(plan_id))

    def live_for_request(self, request_id: UUID) -> PlanRecord | None:
        q = select(ErasurePlanModel).where(
            ErasurePlanModel.request_id == request_id,
            ErasurePlanModel.status.in_([s.value for s in LIVE_PLAN_STATUSES]),
        )
        m = self.session.scalars(q).first()
        return None if m is None else _plan_record(m)

    def latest_for_request(self, request_id: UUID) -> PlanRecord | None:
        q = (
            select(ErasurePlanModel)
            .where(ErasurePlanModel.request_id == request_id)
            .order_by(ErasurePlanModel.version.desc())
            .limit(1)
        )
        m = self.session.scalars(q).first()
        return None if m is None else _plan_record(m)

    def list_for_request(self, request_id: UUID) -> list[PlanRecord]:
        q = (
            select(ErasurePlanModel)
            .where(ErasurePlanModel.request_id == request_id)
            .order_by(ErasurePlanModel.version)
        )
        return [_plan_record(m) for m in self.session.scalars(q)]

    def items(self, plan_id: UUID) -> list[PlanItemRecord]:
        m = self._plan(plan_id)
        return [
            PlanItemRecord(i.id, _item(i), tuple(i.record_key_hmacs))
            for i in sorted(m.items, key=lambda i: i.step_order)
        ]

    def item_ids(self, plan_id: UUID) -> dict[int, UUID]:
        q = select(PlanItemModel.step_order, PlanItemModel.id).where(
            PlanItemModel.plan_id == plan_id
        )
        return {int(s): i for s, i in self.session.execute(q)}

    def conflicts(self, plan_id: UUID) -> list[ConflictRecord]:
        m = self._plan(plan_id)
        return [
            ConflictRecord(c.id, _conflict(c), c.resolution, c.resolved_by)
            for c in sorted(m.conflicts, key=_conflict_key)
        ]

    def set_status(self, plan_id: UUID, status: PlanStatus) -> PlanRecord:
        m = self._plan(plan_id)
        m.status = PlanStatus(status).value
        self.session.flush()
        return _plan_record(m)

    def approve(
        self, plan_id: UUID, approved_by: str, approved_at: datetime, approved_hash: str
    ) -> PlanRecord:
        m = self._plan(plan_id)
        if approved_hash != m.plan_hash:
            raise PlanHashMismatch(
                "approval hash does not match the plan hash", details={"plan_id": str(plan_id)}
            )
        m.status = PlanStatus.APPROVED.value
        m.approved_by = approved_by
        m.approved_at = approved_at
        m.approved_hash = approved_hash
        self.session.flush()
        return _plan_record(m)

    def approval(self, plan_id: UUID) -> Approval | None:
        m = self._plan(plan_id)
        if m.approved_at is None or m.approved_hash is None or m.approved_by is None:
            return None
        return Approval(m.id, m.approved_hash, m.approved_by, m.approved_at)

    def resolve_conflict(
        self, conflict_id: UUID, resolution: str, resolved_by: str
    ) -> ConflictRecord:
        if resolution not in CONFLICT_RESOLUTIONS:
            raise ValidationError("unsupported conflict resolution")
        c = self._get(PlanConflictModel, conflict_id)
        c.resolution = resolution
        c.resolved_by = resolved_by
        self.session.flush()
        return ConflictRecord(c.id, _conflict(c), c.resolution, c.resolved_by)

    def set_plan_hash(self, plan_id: UUID, plan_hash: str) -> PlanRecord:
        m = self._plan(plan_id)
        m.plan_hash = plan_hash
        self.session.flush()
        return _plan_record(m)

    # ------------------------------------------------------------ record graph
    def save_record_dependencies(self, plan_id: UUID, rows: Sequence[RecordDependencyRow]) -> int:
        self._plan(plan_id)
        self.session.add_all(
            RecordDependency(
                plan_id=plan_id,
                parent_table=r.parent_table,
                child_table=r.child_table,
                parent_key=None if r.parent_key is None else key_to_json(r.parent_key),
                child_key=None if r.child_key is None else key_to_json(r.child_key),
                parent_key_hmac=r.parent_key_hmac,
                child_key_hmac=r.child_key_hmac,
                child_role=RecordRole(r.child_role).value,
                edge_id=r.edge_id,
                depth=r.depth,
            )
            for r in rows
        )
        self.session.flush()
        return len(rows)

    def record_dependencies(self, plan_id: UUID) -> list[RecordDependencyRow]:
        q = (
            select(RecordDependency)
            .where(RecordDependency.plan_id == plan_id)
            .order_by(
                RecordDependency.depth,
                RecordDependency.child_table,
                RecordDependency.child_key_hmac,
                RecordDependency.parent_key_hmac,
            )
        )
        return [
            RecordDependencyRow(
                parent_table=m.parent_table,
                child_table=m.child_table,
                parent_key_hmac=m.parent_key_hmac,
                child_key_hmac=m.child_key_hmac,
                child_role=RecordRole(m.child_role),
                depth=m.depth,
                edge_id=m.edge_id,
                parent_key=None if m.parent_key is None else key_from_json(m.parent_key),
                child_key=None if m.child_key is None else key_from_json(m.child_key),
            )
            for m in self.session.scalars(q)
        ]

    def purge_raw_keys(self, plan_id: UUID) -> int:
        """NULL every raw key of the plan (HMACs stay). Returns the number of rows touched."""
        a = self.session.execute(
            update(PlanItemModel)
            .where(PlanItemModel.plan_id == plan_id, PlanItemModel.record_keys.is_not(None))
            .values(record_keys=None)
        )
        b = self.session.execute(
            update(RecordDependency)
            .where(
                RecordDependency.plan_id == plan_id,
                (RecordDependency.parent_key.is_not(None))
                | (RecordDependency.child_key.is_not(None)),
            )
            .values(parent_key=None, child_key=None)
        )
        self.session.expire_all()
        return int(getattr(a, "rowcount", 0) or 0) + int(getattr(b, "rowcount", 0) or 0)
