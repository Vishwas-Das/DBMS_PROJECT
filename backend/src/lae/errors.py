"""Exception hierarchy (docs/module_contracts.md §9.1).

`details` must never contain personal-data values — only table/column names, counts and HMACs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar


class LaeError(Exception):
    default_code: ClassVar[str] = "INTERNAL"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.details: dict[str, Any] = dict(details or {})

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class ConfigurationError(LaeError):
    default_code = "CONFIGURATION"


class NotFoundError(LaeError):
    default_code = "NOT_FOUND"


class SubjectNotFoundError(NotFoundError):
    default_code = "SUBJECT_NOT_FOUND"


class ValidationError(LaeError):
    default_code = "INVALID_INPUT"


class UnknownTableError(ValidationError):
    default_code = "UNKNOWN_TABLE"


class UnknownColumnError(ValidationError):
    default_code = "UNKNOWN_COLUMN"


class CanonicalizationError(ValidationError):
    default_code = "CANONICALIZATION"


class CatalogError(LaeError):
    default_code = "CATALOG"


class DiscoveryLimitExceeded(LaeError):
    default_code = "DISCOVERY_LIMIT"


class GraphIntegrityError(LaeError):
    default_code = "GRAPH_INTEGRITY"


class PlanError(LaeError):
    default_code = "PLAN"


class InvalidStateTransition(PlanError):
    default_code = "INVALID_STATE"


class PlanBlockedError(PlanError):
    default_code = "PLAN_BLOCKED"


class PlanHashMismatch(PlanError):
    default_code = "PLAN_HASH_MISMATCH"


class ExecutionError(LaeError):
    default_code = "EXECUTION"


class StalePlanError(ExecutionError):
    default_code = "STALE_PLAN"


class RowCountMismatch(ExecutionError):
    default_code = "ROW_COUNT_MISMATCH"

    def __init__(self, step_order: int, expected: int, actual: int) -> None:
        super().__init__(
            f"step {step_order}: expected {expected} rows, affected {actual}",
            details={"step_order": step_order, "expected": expected, "actual": actual},
        )
        self.step_order = step_order
        self.expected = expected
        self.actual = actual


class AlreadyExecutedError(ExecutionError):
    default_code = "ALREADY_EXECUTED"


class SerializationConflict(ExecutionError):
    default_code = "SERIALIZATION_CONFLICT"


class TargetQueryError(ExecutionError):
    default_code = "TARGET_QUERY"


class VerificationError(LaeError):
    default_code = "VERIFICATION"


class AuditError(LaeError):
    default_code = "AUDIT_INTEGRITY"


class AuditChainBroken(AuditError):
    pass


class SignatureError(AuditError):
    pass
