"""Plan status machine and conflict acknowledgement (architecture.md §10.7, CR lead-1 §2).

DRAFT -> APPROVED -> EXECUTED -> VERIFIED, with side exits BLOCKED, SUPERSEDED and FAILED.
"""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

from lae.contracts.enums import ConflictCode, PlanStatus
from lae.contracts.types import ErasurePlan
from lae.errors import InvalidStateTransition, PlanBlockedError, PlanError, PlanHashMismatch
from lae.planner.hashing import plan_with_hash

__all__ = [
    "ACKNOWLEDGEABLE_CODES",
    "TRANSITIONS",
    "acknowledge_conflict",
    "check_approval",
    "check_transition",
    "initial_status",
]

TRANSITIONS: MappingProxyType[PlanStatus, frozenset[PlanStatus]] = MappingProxyType(
    {
        PlanStatus.DRAFT: frozenset(
            {PlanStatus.BLOCKED, PlanStatus.APPROVED, PlanStatus.SUPERSEDED}
        ),
        # BLOCKED -> DRAFT once every BLOCKING conflict is resolved (acknowledged).
        PlanStatus.BLOCKED: frozenset({PlanStatus.DRAFT, PlanStatus.SUPERSEDED}),
        # APPROVED -> SUPERSEDED after STALE_PLAN (re-plan); -> FAILED after a failed LIVE run.
        PlanStatus.APPROVED: frozenset(
            {PlanStatus.EXECUTED, PlanStatus.FAILED, PlanStatus.SUPERSEDED}
        ),
        PlanStatus.EXECUTED: frozenset({PlanStatus.VERIFIED, PlanStatus.FAILED}),
        PlanStatus.VERIFIED: frozenset(),
        PlanStatus.SUPERSEDED: frozenset(),
        PlanStatus.FAILED: frozenset(),
    }
)

ACKNOWLEDGEABLE_CODES: frozenset[ConflictCode] = frozenset(
    {
        ConflictCode.OPAQUE_ARTIFACT,
        ConflictCode.SHARED_RECORD,
        ConflictCode.RETAINED_WITH_PII,
        ConflictCode.EXTERNAL_ARTIFACT,
        ConflictCode.NO_PRIMARY_KEY,
    }
)
"""Conflicts a human may accept. All other codes need a policy (or schema) change and a re-plan."""


def check_transition(current: PlanStatus, target: PlanStatus) -> None:
    """Raise InvalidStateTransition unless `current -> target` is allowed."""
    if target not in TRANSITIONS[current]:
        raise InvalidStateTransition(
            f"plan status cannot change from {current.value} to {target.value}",
            details={"from": current.value, "to": target.value},
        )


def initial_status(plan: ErasurePlan) -> PlanStatus:
    """BLOCKED if any BLOCKING conflict is unresolved, else DRAFT."""
    return PlanStatus.BLOCKED if plan.is_blocked else PlanStatus.DRAFT


def check_approval(plan: ErasurePlan, current: PlanStatus, approved_hash: str) -> None:
    """Preconditions of approval: status allows APPROVED, plan not blocked, hash echoed."""
    if plan.is_blocked:
        raise PlanBlockedError(
            "plan has unresolved blocking conflicts",
            details={"plan_id": str(plan.plan_id), "unresolved": _unresolved_codes(plan)},
        )
    check_transition(current, PlanStatus.APPROVED)
    if approved_hash != plan.plan_hash:
        raise PlanHashMismatch(
            "approval hash does not match the plan hash", details={"plan_id": str(plan.plan_id)}
        )


def _unresolved_codes(plan: ErasurePlan) -> list[str]:
    return sorted({c.code.value for c in plan.conflicts if not c.resolved})


def acknowledge_conflict(plan: ErasurePlan, index: int) -> ErasurePlan:
    """Mark conflict `index` as resolved (acknowledged) and recompute the plan hash.

    Raises PlanError if the index is out of range, the conflict is already resolved, or its
    code is not acknowledgeable (those need a policy change and a new plan).
    """
    if not 0 <= index < len(plan.conflicts):
        raise PlanError(
            "conflict index out of range",
            code="CONFLICT_NOT_FOUND",
            details={"index": index, "conflicts": len(plan.conflicts)},
        )
    c = plan.conflicts[index]
    if c.resolved:
        raise PlanError(
            "conflict is already resolved",
            code="CONFLICT_ALREADY_RESOLVED",
            details={"index": index, "code": c.code.value},
        )
    if c.code not in ACKNOWLEDGEABLE_CODES:
        raise PlanError(
            f"conflict {c.code.value} cannot be acknowledged; change the policy and re-plan",
            code="CONFLICT_NOT_ACKNOWLEDGEABLE",
            details={"index": index, "code": c.code.value, "table": c.table.qualified()},
        )
    conflicts = list(plan.conflicts)
    conflicts[index] = replace(c, resolved=True)
    return plan_with_hash(replace(plan, conflicts=tuple(conflicts)))
