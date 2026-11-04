"""Repositories: Session-bound, never commit, convert ORM <-> lae.contracts / lae.db.records.

Each class docstring lists its methods. Missing rows raise `lae.errors.NotFoundError`.
"""

from __future__ import annotations

from lae.db.repositories.artifact import ArtifactRepo
from lae.db.repositories.audit import AuditRepo
from lae.db.repositories.benchmark import BenchmarkRepo
from lae.db.repositories.evidence import EvidenceRepo, SigningKeyRepo
from lae.db.repositories.execution import ExecutionRepo
from lae.db.repositories.plan import PlanRepo
from lae.db.repositories.policy import PolicyRepo
from lae.db.repositories.request import RequestRepo
from lae.db.repositories.snapshot import SnapshotRepo
from lae.db.repositories.verification import VerificationRepo

__all__ = [
    "ArtifactRepo",
    "AuditRepo",
    "BenchmarkRepo",
    "EvidenceRepo",
    "ExecutionRepo",
    "PlanRepo",
    "PolicyRepo",
    "RequestRepo",
    "SigningKeyRepo",
    "SnapshotRepo",
    "VerificationRepo",
]
