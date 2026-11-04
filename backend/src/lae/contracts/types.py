"""Shared, immutable contract types (docs/module_contracts.md §2).

Rules: frozen dataclasses, tuples instead of lists, no ORM/DB/web imports.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from types import MappingProxyType
from typing import Any, Literal
from uuid import UUID

from lae.contracts.enums import (
    ActionCategory,
    ArtifactKind,
    CheckType,
    ConflictCode,
    EdgeOrigin,
    EdgeStatus,
    ErasureStrategy,
    ExecutionMode,
    ExecutionStatus,
    LineageLevel,
    Normalization,
    OnDeleteAction,
    PiiCategory,
    PlanAction,
    RefreshStrategy,
    RetentionAction,
    Severity,
    StepStatus,
    TableKind,
    Traceability,
    Transform,
    VerificationOutcome,
    VerificationStatus,
    action_category,
)
from lae.errors import UnknownColumnError, UnknownTableError, ValidationError

KeyValue = int | str
"""Primary-key values are restricted to int / str in v1 (uuid is represented as str)."""

KeyTuple = tuple[KeyValue, ...]


def _key_sort_token(v: KeyValue) -> tuple[int, int | str]:
    # ints sort before strs; within each type natural order. Deterministic across platforms.
    if isinstance(v, bool):  # bool is an int subclass; reject explicitly
        raise ValidationError("boolean key values are not supported")
    return (0, v) if isinstance(v, int) else (1, v)


def key_sort_key(values: KeyTuple) -> tuple[tuple[int, int | str], ...]:
    return tuple(_key_sort_token(v) for v in values)


# ------------------------------------------------------------------ references


@dataclass(frozen=True, slots=True, order=True)
class TableRef:
    schema: str
    name: str

    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"

    @classmethod
    def parse(cls, qualified: str) -> TableRef:
        parts = qualified.split(".")
        if len(parts) != 2 or not all(parts):
            raise ValidationError(f"expected 'schema.table', got {qualified!r}")
        return cls(parts[0], parts[1])

    def __str__(self) -> str:
        return self.qualified()


@dataclass(frozen=True, slots=True, order=True)
class ColumnRef:
    table: TableRef
    column: str

    def qualified(self) -> str:
        return f"{self.table.qualified()}.{self.column}"


@dataclass(frozen=True, slots=True)
class RecordKey:
    """A concrete row. If is_predicate, matches all rows WHERE columns = values (no-PK tables)."""

    table: TableRef
    columns: tuple[str, ...]
    values: KeyTuple
    is_predicate: bool = False

    def __post_init__(self) -> None:
        if len(self.columns) != len(self.values) or not self.columns:
            raise ValidationError("RecordKey columns and values must be non-empty and equal length")
        key_sort_key(self.values)  # validates value types

    def sort_key(self) -> tuple[Any, ...]:
        return (self.table, self.columns, key_sort_key(self.values), self.is_predicate)

    def as_canonical(self) -> dict[str, Any]:
        return {
            "table": self.table.qualified(),
            "columns": list(self.columns),
            "values": list(self.values),
            "is_predicate": self.is_predicate,
        }


# ------------------------------------------------------------------ catalog


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    name: str
    ordinal: int
    data_type: str
    is_nullable: bool


@dataclass(frozen=True, slots=True)
class TableInfo:
    ref: TableRef
    kind: TableKind
    columns: tuple[ColumnInfo, ...]
    primary_key: tuple[str, ...]
    estimated_rows: int | None = None
    definition_sql: str | None = None

    def column(self, name: str) -> ColumnInfo:
        for c in self.columns:
            if c.name == name:
                return c
        raise UnknownColumnError(
            f"unknown column {self.ref.qualified()}.{name}",
            details={"table": self.ref.qualified(), "column": name},
        )

    def has_column(self, name: str) -> bool:
        return any(c.name == name for c in self.columns)


@dataclass(frozen=True, slots=True, eq=False)
class CatalogSnapshot:
    tables: Mapping[TableRef, TableInfo]
    schemas: tuple[str, ...]
    fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.tables, MappingProxyType):
            object.__setattr__(self, "tables", MappingProxyType(dict(self.tables)))

    def table(self, ref: TableRef) -> TableInfo:
        try:
            return self.tables[ref]
        except KeyError:
            raise UnknownTableError(
                f"unknown table {ref.qualified()}", details={"table": ref.qualified()}
            ) from None

    def column(self, ref: ColumnRef) -> ColumnInfo:
        return self.table(ref.table).column(ref.column)

    def has_table(self, ref: TableRef) -> bool:
        return ref in self.tables


# ------------------------------------------------------------------ lineage


@dataclass(frozen=True, slots=True)
class RelationshipEdge:
    edge_id: UUID
    level: LineageLevel  # TABLE or COLUMN
    origin: EdgeOrigin
    source: TableRef  # upstream (owning) side, e.g. parent table of an FK
    target: TableRef  # downstream (dependent) side, e.g. child table of an FK
    source_columns: tuple[str, ...]
    target_columns: tuple[str, ...]
    transform: Transform
    traceability: Traceability
    status: EdgeStatus
    on_delete: OnDeleteAction | None = None
    constraint_name: str | None = None
    confidence: float = 1.0
    artifact_id: UUID | None = None
    details: Mapping[str, Any] = field(default_factory=dict, compare=False, hash=False)

    def __post_init__(self) -> None:
        if self.level not in (LineageLevel.TABLE, LineageLevel.COLUMN):
            raise ValidationError("RelationshipEdge.level must be TABLE or COLUMN")
        if len(self.source_columns) != len(self.target_columns):
            raise ValidationError("source_columns and target_columns must have equal length")
        if self.level is LineageLevel.COLUMN and not self.source_columns:
            raise ValidationError("COLUMN-level edges need at least one column pair")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValidationError("confidence must be within [0, 1]")


@dataclass(frozen=True, slots=True)
class KeyMapping:
    source: TableRef
    source_columns: tuple[str, ...]
    target_columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArtifactDefinition:
    artifact_id: UUID
    name: str
    kind: ArtifactKind
    target: TableRef
    definition_sql: str | None
    refresh_strategy: RefreshStrategy
    key_mappings: tuple[KeyMapping, ...] = ()


@dataclass(frozen=True, slots=True)
class SqlLineageResult:
    artifact_id: UUID
    edges: tuple[RelationshipEdge, ...]
    traceability: Traceability
    warnings: tuple[str, ...] = ()


# ------------------------------------------------------------------ policy / subject


@dataclass(frozen=True, slots=True)
class PiiColumn:
    column: ColumnRef
    category: PiiCategory
    strategy: ErasureStrategy
    is_confirmed: bool
    use_for_residual_scan: bool = False


@dataclass(frozen=True, slots=True, eq=False)
class PolicySet:
    retention: Mapping[TableRef, RetentionAction] = field(default_factory=dict)
    pii: Mapping[ColumnRef, PiiColumn] = field(default_factory=dict)

    def action_for(self, t: TableRef) -> RetentionAction:
        return self.retention.get(t, RetentionAction.DELETE)


@dataclass(frozen=True, slots=True)
class SubjectRoot:
    root_id: UUID
    table: TableRef
    key_columns: tuple[str, ...]
    identifier_columns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DiscoveryLimits:
    max_depth: int = 10
    max_records: int = 100_000
    batch_size: int = 1_000

    def __post_init__(self) -> None:
        if self.max_depth < 1 or self.max_records < 1 or self.batch_size < 1:
            raise ValidationError("discovery limits must be positive")


@dataclass(frozen=True, slots=True)
class ResidualProbe:
    origin: ColumnRef
    normalization: Normalization
    value_hmac: str


# ------------------------------------------------------------------ plan


@dataclass(frozen=True, slots=True)
class PlanItem:
    step_order: int
    action: PlanAction
    table: TableRef
    lineage_level: LineageLevel  # RECORD or TABLE
    key_columns: tuple[str, ...]
    record_keys: tuple[KeyTuple, ...]
    target_columns: tuple[str, ...] = ()
    expected_row_count: int | None = None
    depends_on: tuple[int, ...] = ()
    reason_edge_ids: tuple[UUID, ...] = ()
    reason: str = ""  # explainable, PII-free sentence (CR lead-1)

    @property
    def category(self) -> ActionCategory:
        return action_category(self.action)

    def __post_init__(self) -> None:
        if self.step_order < 1:
            raise ValidationError("step_order must be >= 1")
        if self.lineage_level not in (LineageLevel.RECORD, LineageLevel.TABLE):
            raise ValidationError("PlanItem.lineage_level must be RECORD or TABLE")
        if self.lineage_level is LineageLevel.RECORD and self.expected_row_count is None:
            raise ValidationError("RECORD-level plan items need expected_row_count")


@dataclass(frozen=True, slots=True)
class PlanConflict:
    code: ConflictCode
    severity: Severity
    table: TableRef
    message: str
    resolved: bool = False


@dataclass(frozen=True, slots=True)
class ErasurePlan:
    plan_id: UUID
    request_id: UUID
    schema_fingerprint: str
    record_set_hash: str
    items: tuple[PlanItem, ...]
    conflicts: tuple[PlanConflict, ...]
    plan_hash: str

    @property
    def is_blocked(self) -> bool:
        return any(c.severity is Severity.BLOCKING and not c.resolved for c in self.conflicts)


@dataclass(frozen=True, slots=True)
class Approval:
    plan_id: UUID
    plan_hash: str
    approved_by: str
    approved_at: datetime


# ------------------------------------------------------------------ execution


@dataclass(frozen=True, slots=True)
class ColumnAssignment:
    """What an UPDATE writes into a column. PSEUDONYM is computed by the writer from the row key."""

    kind: Literal["NULL", "PSEUDONYM"]


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class StepResult:
    step_order: int
    status: StepStatus
    rows_affected: int | None
    expected_rows: int | None
    duration_ms: int


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    mode: ExecutionMode
    status: ExecutionStatus
    steps: tuple[StepResult, ...]
    error: ErrorInfo | None = None


# ------------------------------------------------------------------ verification


@dataclass(frozen=True, slots=True)
class CheckResult:
    check_type: CheckType
    table: TableRef | None
    column: str | None
    step_order: int | None
    passed: bool
    observed: Mapping[str, Any] = field(default_factory=dict, compare=False, hash=False)
    expected: Mapping[str, Any] = field(default_factory=dict, compare=False, hash=False)


@dataclass(frozen=True, slots=True)
class VerificationReport:
    status: VerificationStatus
    checks: tuple[CheckResult, ...]
    outcome: VerificationOutcome = VerificationOutcome.FAILED  # CR lead-1 §3
    required_artifacts: int = 0
    verified_artifacts: int = 0

    @property
    def coverage(self) -> Fraction:
        """verified / required as an exact fraction (1 when nothing is required)."""
        if self.required_artifacts == 0:
            return Fraction(1)
        return Fraction(self.verified_artifacts, self.required_artifacts)


# ------------------------------------------------------------------ audit


@dataclass(frozen=True, slots=True)
class AuditEventRecord:
    seq: int
    event_type: str
    entity_type: str
    entity_id: UUID | None
    actor: str
    occurred_at: datetime
    payload_canonical: str
    prev_hash: str
    event_hash: str


@dataclass(frozen=True, slots=True)
class EvidenceLeaf:
    table: str
    key_hmac: str
    action: PlanAction
    check_types: tuple[CheckType, ...]
    passed: bool

    def as_canonical(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "key_hmac": self.key_hmac,
            "action": self.action.value,
            "check_types": sorted(c.value for c in self.check_types),
            "passed": self.passed,
        }
