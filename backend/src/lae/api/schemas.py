"""API request/response schemas (database_schema.md §6, adapted by CR lead-1 §4).

Conventions: snake_case JSON, extra="forbid", UUID strings, 64-hex hashes, ISO-8601 UTC timestamps.
No schema here ever carries a raw subject key, email or phone after request creation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from lae.contracts.enums import (
    ActionCategory,
    CheckType,
    ConflictCode,
    EdgeOrigin,
    EdgeStatus,
    ExecutionMode,
    ExecutionStatus,
    PlanAction,
    PlanStatus,
    RecordRole,
    RequestStatus,
    Severity,
    StepStatus,
    TableKind,
    Traceability,
    Transform,
    VerificationOutcome,
    VerificationStatus,
)

HEX64 = r"^[0-9a-f]{64}$"


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ------------------------------------------------------------------ errors


class ErrorBody(_Out):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(_Out):
    error: ErrorBody


# ------------------------------------------------------------------ health / stats


class HealthOut(_Out):
    status: Literal["ok", "degraded"]
    database: bool
    pgvector: str | None
    pgcrypto: str | None
    audit_chain_valid: bool | None
    signing_key_id: str | None


class StatsOut(_Out):
    """Every number is a live COUNT from the database. Nothing is cached or estimated."""

    synthetic_customers: int
    erasure_requests: int
    verified_requests: int
    partial_requests: int
    pending_requests: int
    failed_requests: int
    discovered_tables: int
    discovered_edges: int
    registered_artifacts: int
    audit_events: int
    latest_snapshot_id: UUID | None
    latest_snapshot_at: datetime | None


class CustomerOut(_Out):
    id: int
    display_name: str  # synthetic name
    email_masked: str  # e.g. "a***@example.test"
    open_request_id: UUID | None


# ------------------------------------------------------------------ requests


KeyItem = Annotated[int, Field(ge=-(2**63), le=2**63 - 1)] | Annotated[str, Field(max_length=128)]


class ErasureRequestCreate(_In):
    subject_root: str = Field(default="customer", max_length=63, pattern=r"^[a-z_][a-z0-9_]*$")
    subject_key: list[KeyItem] = Field(min_length=1, max_length=8)
    reason: str | None = Field(default=None, max_length=500)


class ErasureRequestOut(_Out):
    id: UUID
    subject_root: str
    subject_key_hmac: str
    status: RequestStatus
    requested_by: str
    created_at: datetime
    completed_at: datetime | None
    current_plan_id: UUID | None
    latest_outcome: VerificationOutcome | None


# ------------------------------------------------------------------ discovery / lineage


class DiscoveryOut(_Out):
    request_id: UUID
    snapshot_id: UUID
    schema_fingerprint: str
    tables: int
    table_edges: int
    column_edges: int
    records_found: int
    records_by_role: dict[RecordRole, int]
    affected_artifacts: dict[str, Traceability]
    external_dependencies: list[str]
    record_set_hash: str
    duration_ms: int
    warnings: list[str]


class GraphNodeOut(_Out):
    id: str
    kind: Literal["table", "column"]
    schema_name: str
    table_name: str
    column_name: str | None = None
    table_kind: TableKind | None = None
    pii_category: str | None = None
    is_artifact: bool = False


class GraphEdgeOut(_Out):
    id: UUID
    source: str
    target: str
    level: Literal["TABLE", "COLUMN"]
    origin: EdgeOrigin
    transform: Transform
    traceability: Traceability
    status: EdgeStatus
    source_columns: list[str]
    target_columns: list[str]
    on_delete_action: str | None = None
    confidence: float = 1.0
    evidence: dict[str, Any] = Field(default_factory=dict)


class EdgeStatusUpdate(_In):
    status: Literal["CONFIRMED", "REJECTED"]


class SchemaGraphOut(_Out):
    snapshot_id: UUID
    level: Literal["TABLE", "COLUMN"]
    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]


class RecordNodeOut(_Out):
    id: str  # key HMAC — never the raw key
    table: str
    role: RecordRole
    depth: int
    is_predicate: bool = False


class RecordEdgeOut(_Out):
    source: str
    target: str
    edge_id: UUID | None


class LineageOut(_Out):
    request_id: UUID
    plan_id: UUID | None
    snapshot_id: UUID
    schema_graph: SchemaGraphOut
    records: list[RecordNodeOut]
    record_edges: list[RecordEdgeOut]


# ------------------------------------------------------------------ plans


class PlanGenerate(_In):
    max_depth: int = Field(default=10, ge=1, le=50)
    max_records: int = Field(default=100_000, ge=1, le=10_000_000)


class PlanItemOut(_Out):
    id: UUID
    step_order: int
    action: PlanAction
    category: ActionCategory
    schema_name: str
    table_name: str
    lineage_level: Literal["RECORD", "TABLE"]
    key_columns: list[str]
    record_count: int
    record_key_hmacs: list[str]
    target_columns: list[str]
    expected_row_count: int | None
    depends_on_steps: list[int]
    reason_edge_ids: list[UUID]
    reason: str
    retention_policy: str | None


class PlanConflictOut(_Out):
    id: UUID
    code: ConflictCode
    severity: Severity
    schema_name: str
    table_name: str
    message: str
    resolution: str | None
    acknowledgeable: bool


class PlanOut(_Out):
    id: UUID
    request_id: UUID
    version: int
    status: PlanStatus
    snapshot_id: UUID
    schema_fingerprint: str
    record_set_hash: str
    plan_hash: str
    record_count: int
    items: list[PlanItemOut]
    conflicts: list[PlanConflictOut]
    counts_by_category: dict[ActionCategory, int]
    is_blocked: bool
    created_at: datetime
    approved_by: str | None
    approved_at: datetime | None


class PlanApprove(_In):
    plan_hash: str = Field(pattern=HEX64)


class ConflictAcknowledge(_In):
    note: str | None = Field(default=None, max_length=500)


class PlanExecute(_In):
    mode: ExecutionMode


# ------------------------------------------------------------------ execution / verification


class StepResultOut(_Out):
    step_order: int
    action: PlanAction
    category: ActionCategory
    table: str
    status: StepStatus
    rows_affected: int | None
    expected_rows: int | None
    duration_ms: int


class CheckOut(_Out):
    check_type: CheckType
    schema_name: str | None
    table_name: str | None
    column_name: str | None
    step_order: int | None
    passed: bool
    observed: dict[str, Any]
    expected: dict[str, Any]


class ResidualHitOut(_Out):
    table: str
    column: str
    count: int


class VerificationOut(_Out):
    id: UUID
    execution_run_id: UUID
    status: VerificationStatus
    outcome: VerificationOutcome | None
    required_artifacts: int
    verified_artifacts: int
    coverage: float  # display only; exact values are required/verified
    checks: list[CheckOut]
    residual_hits: list[ResidualHitOut]
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None


class ExecutionOut(_Out):
    id: UUID
    plan_id: UUID
    mode: ExecutionMode
    status: ExecutionStatus
    started_at: datetime
    finished_at: datetime | None
    steps: list[StepResultOut]
    error: ErrorBody | None
    verification: VerificationOut | None = None
    evidence_id: UUID | None = None
    timings_ms: dict[str, int] = Field(default_factory=dict)


# ------------------------------------------------------------------ audit / evidence


class AuditEventOut(_Out):
    seq: int
    event_id: UUID
    occurred_at: datetime
    event_type: str
    entity_type: str
    entity_id: UUID | None
    actor: str
    payload: dict[str, Any]
    prev_hash: str
    event_hash: str


class ChainVerificationOut(_Out):
    valid: bool
    checked_events: int
    head_seq: int | None
    head_hash: str | None
    first_invalid_seq: int | None
    reason: str | None = None


class EvidenceOut(_Out):
    id: UUID
    plan_id: UUID
    merkle_root: str
    leaf_count: int
    chain_anchor_seq: int
    chain_anchor_hash: str
    signing_key_id: str
    issued_at: datetime
    signature_valid: bool
    bundle: dict[str, Any]
    signature: str


class AuditOut(_Out):
    request_id: UUID
    events: list[AuditEventOut]
    chain: ChainVerificationOut
    evidence: EvidenceOut | None


class BenchmarkRunOut(_Out):
    id: UUID
    scenario: str
    params: dict[str, Any]
    seed: int
    git_commit: str | None
    environment: dict[str, Any]
    metrics: dict[str, Any]
    created_at: datetime
