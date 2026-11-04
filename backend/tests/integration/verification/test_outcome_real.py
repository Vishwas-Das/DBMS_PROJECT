"""Outcome and coverage on real data (CR lead-1 §3): VERIFIED / PARTIAL / FAILED."""

from __future__ import annotations

from fractions import Fraction
from typing import Any

import pytest

from lae.contracts.enums import (
    ActionCategory,
    CheckType,
    PlanAction,
    VerificationOutcome,
    VerificationStatus,
)
from lae.contracts.types import TableRef
from lae.verification import coverage, item_verified

pytestmark = pytest.mark.integration

CRM_EXPORT = TableRef("external", "crm_export_files")


def run(w: Any, plan: Any, **kwargs: Any) -> Any:
    return w.verify(
        plan, probes=w.capture_probes(), scan=w.scan_columns(), rediscover=w.rediscover, **kwargs
    )


def test_verified(world: Any) -> None:
    w = world
    plan = w.subject_plan(hidden_copies=True)
    probes, scan = w.capture_probes(), w.scan_columns()
    w.apply(plan)
    r = w.verify(plan, probes=probes, scan=scan, rediscover=w.rediscover)
    assert (r.outcome, r.status) == (VerificationOutcome.VERIFIED, VerificationStatus.PASSED)
    assert r.required_artifacts == r.verified_artifacts == len(plan.items) == 19
    assert r.coverage == 1 and coverage(r.verified_artifacts, r.required_artifacts) == 1
    assert all(item_verified(i, r.checks) for i in plan.items)
    # every check type that this plan can produce was exercised on real data
    assert {c.check_type for c in r.checks} == set(CheckType)


def test_partial_with_external_item(world: Any) -> None:
    w = world
    plan = w.subject_plan(hidden_copies=True, external=True)
    probes, scan = w.capture_probes(), w.scan_columns()
    w.apply(plan)
    r = w.verify(plan, probes=probes, scan=scan, rediscover=w.rediscover)
    ext = [i for i in plan.items if i.table == CRM_EXPORT]
    assert len(ext) == 1 and ext[0].action is PlanAction.SCHEDULE_EXTERNAL
    assert ext[0].category is ActionCategory.SCHEDULE
    assert not [c for c in r.checks if c.step_order == ext[0].step_order]  # never checkable
    assert all(c.passed for c in r.checks)
    assert (r.outcome, r.status) == (VerificationOutcome.PARTIAL, VerificationStatus.PASSED)
    assert (r.verified_artifacts, r.required_artifacts) == (19, 20)
    assert r.coverage == Fraction(19, 20)
    assert coverage(r.verified_artifacts, r.required_artifacts) == Fraction(19, 20)


def test_failed_when_a_step_was_not_applied(world: Any) -> None:
    """Skipping the anonymization fails its checks: FAILED and coverage < 1."""
    w = world
    plan = w.subject_plan(hidden_copies=True, external=True)
    anon = next(i for i in plan.items if i.action is PlanAction.ANONYMIZE_ROWS)
    probes, scan = w.capture_probes(), w.scan_columns()
    w.apply(plan, skip=[anon.step_order])
    r = w.verify(plan, probes=probes, scan=scan, rediscover=w.rediscover)
    failed = {(c.check_type, c.step_order, c.column) for c in r.checks if not c.passed}
    assert (CheckType.COLUMNS_ANONYMIZED, anon.step_order, "shipping_name") in failed
    assert (CheckType.COLUMNS_ANONYMIZED, anon.step_order, "shipping_address") in failed
    # the still-present shipping_name copies are also found by the residual scan
    residual = next(c for c in r.checks if c.check_type is CheckType.RESIDUAL_VALUE_SCAN)
    assert residual.observed["hits"] == [
        {"table": "app.purchase_order", "column": "shipping_name", "count": len(anon.record_keys)}
    ]
    assert (r.outcome, r.status) == (VerificationOutcome.FAILED, VerificationStatus.FAILED)
    assert (r.verified_artifacts, r.required_artifacts) == (18, 20)
    assert r.coverage == Fraction(18, 20)


def test_failed_by_residual_scan_alone_keeps_full_coverage(world: Any) -> None:
    w = world
    plan = w.subject_plan()
    probes, scan = w.capture_probes(), w.scan_columns()
    w.apply(plan)
    r = w.verify(plan, probes=probes, scan=scan, rediscover=w.rediscover)
    assert r.outcome is VerificationOutcome.FAILED and r.coverage == 1
    assert [c.check_type for c in r.checks if not c.passed] == [CheckType.RESIDUAL_VALUE_SCAN]


def test_before_execution_almost_nothing_verifies(world: Any) -> None:
    """Sanity: verifying the untouched database fails, and only the 'keep' items count."""
    w = world
    plan = w.subject_plan(hidden_copies=True)
    r = run(w, plan)
    assert r.outcome is VerificationOutcome.FAILED
    redisc = next(c for c in r.checks if c.check_type is CheckType.REDISCOVERY_EMPTY)
    assert not redisc.passed and redisc.observed["subject_root_found"] is True
    verified = {i.action for i in plan.items if item_verified(i, r.checks)}
    assert verified <= {PlanAction.RETAIN_ROWS, PlanAction.REFRESH_MATVIEW}
