"""Outcome and coverage rules (CR lead-1 §3).

- Required artifacts = every plan item.
- An item is verified iff its category is verifiable (not SCHEDULE / MANUAL_REVIEW /
  UNRESOLVED), at least one check is bound to it (``step_order``), and all bound checks passed.
- ``FAILED`` if any check failed; ``VERIFIED`` iff nothing failed and coverage == 1; otherwise
  ``PARTIAL``. Run status ``PASSED`` iff no check failed (``ERROR`` is set by the service when
  ``VerificationError`` is raised).
"""

from __future__ import annotations

from collections.abc import Sequence

from lae.contracts.enums import UNVERIFIABLE_CATEGORIES, VerificationOutcome, VerificationStatus
from lae.contracts.types import CheckResult, ErasurePlan, PlanItem, VerificationReport


def item_verified(item: PlanItem, checks: Sequence[CheckResult]) -> bool:
    if item.category in UNVERIFIABLE_CATEGORIES:
        return False
    bound = [c for c in checks if c.step_order == item.step_order]
    return bool(bound) and all(c.passed for c in bound)


def build_report(plan: ErasurePlan, checks: Sequence[CheckResult]) -> VerificationReport:
    required = len(plan.items)
    verified = sum(1 for it in plan.items if item_verified(it, checks))
    failed = any(not c.passed for c in checks)
    status = VerificationStatus.FAILED if failed else VerificationStatus.PASSED
    report = VerificationReport(
        status=status,
        checks=tuple(checks),
        outcome=VerificationOutcome.FAILED,
        required_artifacts=required,
        verified_artifacts=verified,
    )
    if failed:
        return report
    outcome = VerificationOutcome.VERIFIED if report.coverage == 1 else VerificationOutcome.PARTIAL
    return VerificationReport(status, tuple(checks), outcome, required, verified)
