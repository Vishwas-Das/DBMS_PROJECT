"""Outcome/coverage rules (CR lead-1 §3) and their agreement with the evidence leaves."""

from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest

from lae.audit.evidence import leaves_from_verification
from lae.audit.hmac_keys import HmacKeyring
from lae.contracts.enums import (
    CheckType,
    LineageLevel,
    PlanAction,
    VerificationOutcome,
    VerificationStatus,
)
from lae.contracts.types import CheckResult
from lae.verification import build_report, item_verified

pytestmark = pytest.mark.unit


def chk(step: int | None, passed: bool, ct: CheckType = CheckType.ROW_ABSENT) -> CheckResult:
    return CheckResult(ct, None, None, step, passed)


@pytest.mark.parametrize("action", list(PlanAction))
def test_item_verified_rule(h: ModuleType, action: PlanAction) -> None:
    it = h.item(1, action, h.ORDER, keys=((1,),))
    unverifiable = action in (
        PlanAction.SCHEDULE_EXTERNAL,
        PlanAction.MANUAL_REVIEW,
        PlanAction.UNRESOLVED,
    )
    assert item_verified(it, [chk(1, True)]) is not unverifiable
    assert item_verified(it, [chk(1, True), chk(1, False)]) is False
    assert item_verified(it, []) is False  # no bound check => not verified
    assert item_verified(it, [chk(2, True)]) is False  # bound by step_order only


def test_build_report_status_and_outcome(h: ModuleType) -> None:
    plan = h.make_plan([h.item(1, PlanAction.DELETE_ROWS, h.ORDER, keys=((1,),))])
    r = build_report(plan, [chk(1, True), chk(None, True, CheckType.RESIDUAL_VALUE_SCAN)])
    assert (r.status, r.outcome, r.verified_artifacts) == (
        VerificationStatus.PASSED,
        VerificationOutcome.VERIFIED,
        1,
    )
    r = build_report(plan, [chk(1, True), chk(None, False, CheckType.RESIDUAL_VALUE_SCAN)])
    assert (r.status, r.outcome, r.verified_artifacts) == (
        VerificationStatus.FAILED,
        VerificationOutcome.FAILED,
        1,
    )
    r = build_report(plan, [chk(None, True, CheckType.RESIDUAL_VALUE_SCAN)])
    assert (r.status, r.outcome, r.verified_artifacts) == (
        VerificationStatus.PASSED,
        VerificationOutcome.PARTIAL,
        0,
    )


def test_leaves_agree_with_coverage(world: Any, h: ModuleType) -> None:
    w = world
    w.items.append(h.item(10, PlanAction.UNRESOLVED, h.t("ext"), level=LineageLevel.TABLE))
    w.rows(h.CACHE).append({"customer_id": h.SUBJECT, "payload": "{}"})
    report = w.verify()
    leaves = leaves_from_verification(w.plan, report, HmacKeyring(h.HMAC_KEY))
    by_step = {it.step_order: item_verified(it, report.checks) for it in w.plan.items}
    assert sum(by_step.values()) == report.verified_artifacts == 8
    for it in w.plan.items:
        for leaf in leaves:
            if leaf.table == it.table.qualified() and leaf.action is it.action:
                assert leaf.passed is by_step[it.step_order]
