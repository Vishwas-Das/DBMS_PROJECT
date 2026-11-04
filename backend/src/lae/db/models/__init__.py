"""SQLAlchemy 2.x models for the `lae` schema (database_schema.md §3, §5, §7).

Importing this package registers every table on `Base.metadata`. ORM classes never leave
`lae.db`: repositories convert them to `lae.contracts` types or `lae.db.records` rows.
"""

from __future__ import annotations

from lae.db.models.audit import AuditEvent, EvidenceBundle, SigningKey
from lae.db.models.base import NAMING_CONVENTION, SCHEMA, Base
from lae.db.models.bench import BenchmarkRun
from lae.db.models.catalog import CatalogColumn, CatalogTable, LineageEdge, LineageSnapshot
from lae.db.models.config import (
    DerivedArtifact,
    PiiClassification,
    RetentionPolicyModel,
    SubjectRootModel,
)
from lae.db.models.workflow import (
    ErasurePlanModel,
    ErasureRequest,
    ExecutionRun,
    ExecutionStepResult,
    PlanConflictModel,
    PlanItemModel,
    RecordDependency,
    ResidualProbeModel,
    VerificationCheck,
    VerificationRun,
)

__all__ = [
    "NAMING_CONVENTION",
    "SCHEMA",
    "AuditEvent",
    "Base",
    "BenchmarkRun",
    "CatalogColumn",
    "CatalogTable",
    "DerivedArtifact",
    "ErasurePlanModel",
    "ErasureRequest",
    "EvidenceBundle",
    "ExecutionRun",
    "ExecutionStepResult",
    "LineageEdge",
    "LineageSnapshot",
    "PiiClassification",
    "PlanConflictModel",
    "PlanItemModel",
    "RecordDependency",
    "ResidualProbeModel",
    "RetentionPolicyModel",
    "SigningKey",
    "SubjectRootModel",
    "VerificationCheck",
    "VerificationRun",
]
