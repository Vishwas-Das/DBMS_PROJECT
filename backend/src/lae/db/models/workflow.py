"""Erasure workflow tables (database_schema.md §3.3 + §7)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lae.contracts.enums import (
    ActionCategory,
    CheckType,
    ConflictCode,
    ExecutionMode,
    ExecutionStatus,
    LineageLevel,
    Normalization,
    PlanAction,
    PlanStatus,
    RecordRole,
    RequestStatus,
    Severity,
    StepStatus,
    VerificationOutcome,
    VerificationStatus,
)
from lae.db.models.base import (
    Base,
    created_at,
    enum_check,
    hex_check,
    in_list,
    uuid_pk,
)

PLAN_ITEM_LEVELS = (LineageLevel.RECORD, LineageLevel.TABLE)
CONFLICT_RESOLUTIONS = ("ACKNOWLEDGED", "POLICY_CHANGED")
LIVE_PLAN_STATUSES = (PlanStatus.DRAFT, PlanStatus.BLOCKED, PlanStatus.APPROVED)
APPROVED_PLAN_STATUSES = (PlanStatus.APPROVED, PlanStatus.EXECUTED, PlanStatus.VERIFIED)


class ErasureRequest(Base):
    __tablename__ = "erasure_request"

    id: Mapped[UUID] = uuid_pk()
    subject_root_id: Mapped[UUID] = mapped_column(
        ForeignKey("lae.subject_root.id", ondelete="RESTRICT"), nullable=False
    )
    subject_key: Mapped[list[Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    subject_key_hmac: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at()
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        enum_check("status", RequestStatus),
        hex_check("subject_key_hmac"),
        CheckConstraint(
            "status IN ('VERIFIED', 'CANCELLED') OR subject_key IS NOT NULL",
            name="subject_key_until_terminal",
        ),
        Index(None, "subject_root_id", "subject_key_hmac"),
    )


class ResidualProbeModel(Base):
    __tablename__ = "residual_probe"

    id: Mapped[UUID] = uuid_pk()
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("lae.erasure_request.id", ondelete="CASCADE"), nullable=False
    )
    origin_schema: Mapped[str] = mapped_column(Text, nullable=False)
    origin_table: Mapped[str] = mapped_column(Text, nullable=False)
    origin_column: Mapped[str] = mapped_column(Text, nullable=False)
    normalization: Mapped[str] = mapped_column(Text, nullable=False)
    value_hmac: Mapped[str] = mapped_column(CHAR(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("request_id", "value_hmac", "normalization"),
        enum_check("normalization", Normalization),
        hex_check("value_hmac"),
    )


class ErasurePlanModel(Base):
    __tablename__ = "erasure_plan"

    id: Mapped[UUID] = uuid_pk()
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("lae.erasure_request.id", ondelete="RESTRICT"), nullable=False
    )
    snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("lae.lineage_snapshot.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    schema_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    record_set_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    plan_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = created_at()
    approved_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    approved_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)

    items: Mapped[list[PlanItemModel]] = relationship(
        order_by="PlanItemModel.step_order", cascade="all, delete-orphan", passive_deletes=True
    )
    conflicts: Mapped[list[PlanConflictModel]] = relationship(
        order_by="PlanConflictModel.id", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        UniqueConstraint("request_id", "version"),
        enum_check("status", PlanStatus),
        hex_check("schema_fingerprint"),
        hex_check("record_set_hash"),
        hex_check("plan_hash"),
        hex_check("approved_hash", nullable=True),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("record_count >= 0", name="record_count_nonneg"),
        CheckConstraint(
            # migration 0002: SUPERSEDED/FAILED plans may keep their approval history
            f"CASE WHEN status IN ({in_list(APPROVED_PLAN_STATUSES)}) THEN approved_at IS NOT NULL "
            "WHEN status IN ('DRAFT','BLOCKED') THEN approved_at IS NULL ELSE true END",
            name="approval_consistent",
        ),
        CheckConstraint(
            "approved_hash IS NULL OR approved_hash = plan_hash", name="approved_hash_matches"
        ),
        Index(
            "uq_erasure_plan_active",
            "request_id",
            unique=True,
            postgresql_where=text(f"status IN ({in_list(LIVE_PLAN_STATUSES)})"),
        ),
    )


class PlanItemModel(Base):
    __tablename__ = "plan_item"

    id: Mapped[UUID] = uuid_pk()
    plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("lae.erasure_plan.id", ondelete="CASCADE"), nullable=False
    )
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    schema_name: Mapped[str] = mapped_column(Text, nullable=False)
    table_name: Mapped[str] = mapped_column(Text, nullable=False)
    lineage_level: Mapped[str] = mapped_column(Text, nullable=False)
    key_columns: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    record_keys: Mapped[list[list[Any]] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    record_key_hmacs: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    target_columns: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    expected_row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    depends_on_steps: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, server_default=text("'{}'::integer[]")
    )
    reason_edge_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )

    __table_args__ = (
        UniqueConstraint("plan_id", "step_order"),
        enum_check("action", PlanAction),
        enum_check("category", ActionCategory),
        enum_check("lineage_level", PLAN_ITEM_LEVELS),
        CheckConstraint("step_order >= 1", name="step_order_positive"),
        CheckConstraint(
            "expected_row_count IS NULL OR expected_row_count >= 0", name="expected_rows_nonneg"
        ),
        CheckConstraint(
            "lineage_level <> 'RECORD' OR expected_row_count IS NOT NULL",
            name="record_level_count",
        ),
    )


class PlanConflictModel(Base):
    __tablename__ = "plan_conflict"

    id: Mapped[UUID] = uuid_pk()
    plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("lae.erasure_plan.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    schema_name: Mapped[str] = mapped_column(Text, nullable=False)
    table_name: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        enum_check("code", ConflictCode),
        enum_check("severity", Severity),
        CheckConstraint(
            f"resolution IS NULL OR resolution IN ({in_list(CONFLICT_RESOLUTIONS)})",
            name="resolution_valid",
        ),
    )


class RecordDependency(Base):
    __tablename__ = "record_dependency"

    id: Mapped[UUID] = uuid_pk()
    plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("lae.erasure_plan.id", ondelete="CASCADE"), nullable=False
    )
    parent_table: Mapped[str] = mapped_column(Text, nullable=False)
    child_table: Mapped[str] = mapped_column(Text, nullable=False)
    parent_key: Mapped[list[Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    child_key: Mapped[list[Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    parent_key_hmac: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    child_key_hmac: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    child_role: Mapped[str] = mapped_column(Text, nullable=False)
    edge_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("lae.lineage_edge.id", ondelete="RESTRICT"), nullable=True
    )
    depth: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    __table_args__ = (
        enum_check("child_role", RecordRole),
        hex_check("parent_key_hmac"),
        hex_check("child_key_hmac"),
        CheckConstraint("depth >= 0", name="depth_nonneg"),
        CheckConstraint("edge_id IS NOT NULL OR child_role = 'SUBJECT_ROOT'", name="edge_required"),
        Index(None, "plan_id", "child_table"),
        Index(None, "plan_id", "parent_key_hmac"),
    )


class ExecutionRun(Base):
    __tablename__ = "execution_run"

    id: Mapped[UUID] = uuid_pk()
    plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("lae.erasure_plan.id", ondelete="RESTRICT"), nullable=False
    )
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    isolation_level: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'SERIALIZABLE'::text")
    )
    started_at: Mapped[datetime] = created_at()
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)

    __table_args__ = (
        enum_check("mode", ExecutionMode),
        enum_check("status", ExecutionStatus),
        CheckConstraint("mode <> 'DRY_RUN' OR status <> 'COMMITTED'", name="dry_run_never_commits"),
        Index(
            "uq_execution_run_live_committed",
            "plan_id",
            unique=True,
            postgresql_where=text("mode = 'LIVE' AND status = 'COMMITTED'"),
        ),
    )


class ExecutionStepResult(Base):
    __tablename__ = "execution_step_result"

    id: Mapped[UUID] = uuid_pk()
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("lae.execution_run.id", ondelete="CASCADE"), nullable=False
    )
    plan_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("lae.plan_item.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    rows_affected: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("run_id", "plan_item_id"),
        enum_check("status", StepStatus),
        CheckConstraint("duration_ms >= 0", name="duration_nonneg"),
    )


class VerificationRun(Base):
    __tablename__ = "verification_run"

    id: Mapped[UUID] = uuid_pk()
    execution_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("lae.execution_run.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = created_at()
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    required_artifacts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verified_artifacts: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        enum_check("status", VerificationStatus),
        enum_check("outcome", VerificationOutcome, nullable=True),
        CheckConstraint("verified_artifacts <= required_artifacts", name="coverage_bounded"),
    )


class VerificationCheck(Base):
    __tablename__ = "verification_check"

    id: Mapped[UUID] = uuid_pk()
    verification_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("lae.verification_run.id", ondelete="CASCADE"), nullable=False
    )
    check_type: Mapped[str] = mapped_column(Text, nullable=False)
    schema_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    table_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    column_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_item_id: Mapped[UUID | None] = mapped_column(ForeignKey("lae.plan_item.id"), nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    observed: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expected: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        enum_check("check_type", CheckType),
        Index(None, "verification_run_id", "passed"),
    )
