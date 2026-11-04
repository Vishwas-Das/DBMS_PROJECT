"""Immutable row types returned by repositories for entities that have no `lae.contracts` type.

ORM objects never leave `lae.db`; repositories return either contract types or these records.
None of them carries raw personal data except where noted (`RecordDependencyRow` raw keys,
which are purged after evidence).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from lae.contracts.enums import (
    ExecutionMode,
    ExecutionStatus,
    PiiSource,
    PlanStatus,
    RecordRole,
    RequestStatus,
    RetentionAction,
    SnapshotStatus,
    VerificationOutcome,
    VerificationStatus,
)
from lae.contracts.types import (
    ArtifactDefinition,
    ErrorInfo,
    ExecutionResult,
    KeyTuple,
    PiiColumn,
    PlanConflict,
    PlanItem,
    StepResult,
    SubjectRoot,
    TableRef,
    VerificationReport,
)


@dataclass(frozen=True, slots=True)
class SnapshotRecord:
    id: UUID
    created_at: datetime
    status: SnapshotStatus
    scanned_schemas: tuple[str, ...]
    schema_fingerprint: str
    table_count: int
    edge_count: int
    warnings: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    definition: ArtifactDefinition
    is_auto_registered: bool
    is_active: bool
    is_inspectable: bool
    created_at: datetime
    updated_at: datetime

    @property
    def id(self) -> UUID:
        return self.definition.artifact_id


@dataclass(frozen=True, slots=True)
class PiiRecord:
    id: UUID
    pii: PiiColumn
    source: PiiSource


@dataclass(frozen=True, slots=True)
class RetentionRecord:
    id: UUID
    table: TableRef
    action: RetentionAction
    legal_basis: str | None


@dataclass(frozen=True, slots=True)
class SubjectRootRecord:
    name: str
    root: SubjectRoot

    @property
    def id(self) -> UUID:
        return self.root.root_id


@dataclass(frozen=True, slots=True)
class RequestRecord:
    """An erasure request. The raw subject key is NOT included (see RequestRepo.subject_key)."""

    id: UUID
    subject_root_id: UUID
    subject_key_hmac: str
    status: RequestStatus
    requested_by: str
    reason: str | None
    created_at: datetime
    completed_at: datetime | None
    has_subject_key: bool


@dataclass(frozen=True, slots=True)
class PlanRecord:
    id: UUID
    request_id: UUID
    snapshot_id: UUID
    version: int
    status: PlanStatus
    schema_fingerprint: str
    record_set_hash: str
    plan_hash: str
    record_count: int
    created_at: datetime
    approved_by: str | None
    approved_at: datetime | None
    approved_hash: str | None


@dataclass(frozen=True, slots=True)
class PlanItemRecord:
    id: UUID
    item: PlanItem
    record_key_hmacs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConflictRecord:
    id: UUID
    conflict: PlanConflict
    resolution: str | None
    resolved_by: str | None


@dataclass(frozen=True, slots=True)
class RecordDependencyRow:
    """One persisted record-graph edge. Raw keys are None after purge."""

    parent_table: str
    child_table: str
    parent_key_hmac: str
    child_key_hmac: str
    child_role: RecordRole
    depth: int
    edge_id: UUID | None = None
    parent_key: KeyTuple | None = None
    child_key: KeyTuple | None = None


@dataclass(frozen=True, slots=True)
class ExecutionRunRecord:
    id: UUID
    plan_id: UUID
    mode: ExecutionMode
    status: ExecutionStatus
    isolation_level: str
    started_at: datetime
    finished_at: datetime | None
    error: ErrorInfo | None
    steps: tuple[StepResult, ...] = ()

    @property
    def result(self) -> ExecutionResult:
        return ExecutionResult(self.mode, self.status, self.steps, self.error)


@dataclass(frozen=True, slots=True)
class VerificationRunRecord:
    id: UUID
    execution_run_id: UUID
    status: VerificationStatus
    outcome: VerificationOutcome | None
    required_artifacts: int | None
    verified_artifacts: int | None
    started_at: datetime
    finished_at: datetime | None
    report: VerificationReport


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    id: UUID
    plan_id: UUID
    verification_run_id: UUID
    merkle_root: str
    leaf_count: int
    leaves_canonical: str
    chain_anchor_seq: int
    chain_anchor_hash: str
    bundle_canonical: str
    signature: str
    signing_key_id: str
    issued_at: datetime


@dataclass(frozen=True, slots=True)
class SigningKeyRecord:
    key_id: str
    algorithm: str
    public_key_pem: str
    created_at: datetime
    retired_at: datetime | None


@dataclass(frozen=True, slots=True)
class BenchmarkRunRecord:
    id: UUID
    scenario: str
    seed: int
    git_commit: str | None
    created_at: datetime
    params: Mapping[str, Any] = field(default_factory=dict)
    environment: Mapping[str, Any] = field(default_factory=dict)
    metrics: Mapping[str, Any] = field(default_factory=dict)
