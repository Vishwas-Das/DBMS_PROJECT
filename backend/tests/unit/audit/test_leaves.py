"""leaves_from_verification: one leaf per record key / table item, HMAC only, round trip."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from lae.audit.canonical import canonical_json
from lae.audit.evidence import (
    ChainAnchor,
    ExecutionEvidence,
    build_evidence,
    leaves_from_verification,
    verify_evidence_offline,
)
from lae.audit.hmac_keys import HmacKeyring
from lae.audit.merkle import MerkleTree, verify_proof
from lae.audit.signing import Signer
from lae.contracts.enums import (
    CheckType,
    ExecutionMode,
    ExecutionStatus,
    LineageLevel,
    PlanAction,
    StepStatus,
    VerificationOutcome,
    VerificationStatus,
)
from lae.contracts.types import (
    CheckResult,
    ErasurePlan,
    ExecutionResult,
    PlanItem,
    RecordKey,
    StepResult,
    TableRef,
    VerificationReport,
)

pytestmark = pytest.mark.unit

KEY = bytes(range(100, 132))
CUSTOMER = TableRef("app", "customer")
ORDER = TableRef("app", "purchase_order")
LTV = TableRef("app", "customer_ltv")
EXT = TableRef("app", "crm_export")


def _plan() -> ErasurePlan:
    items = (
        PlanItem(1, PlanAction.EXPECT_CASCADE, ORDER, LineageLevel.RECORD, ("id",),
                 ((101,), (100,)), (), 2),
        PlanItem(2, PlanAction.DELETE_ROWS, CUSTOMER, LineageLevel.RECORD, ("id",),
                 (("c-4242",),), (), 1),
        PlanItem(3, PlanAction.REFRESH_MATVIEW, LTV, LineageLevel.TABLE, (), ()),
        PlanItem(4, PlanAction.UNRESOLVED, EXT, LineageLevel.TABLE, (), ()),
    )  # fmt: skip
    return ErasurePlan(UUID(int=1), UUID(int=2), "a" * 64, "b" * 64, items, (), "c" * 64)


def _report(row_absent_ok: bool = True) -> VerificationReport:
    checks = (
        CheckResult(CheckType.CASCADE_COMPLETE, ORDER, None, 1, True),
        CheckResult(CheckType.ROW_ABSENT, CUSTOMER, None, 2, row_absent_ok),
        CheckResult(CheckType.DERIVED_CONSISTENT, LTV, None, 3, True),
        CheckResult(CheckType.ROW_ABSENT, LTV, None, 3, True),
        CheckResult(CheckType.RESIDUAL_VALUE_SCAN, None, None, None, True),
    )
    return VerificationReport(VerificationStatus.PASSED, checks, VerificationOutcome.PARTIAL, 4, 3)


@pytest.fixture
def ring() -> HmacKeyring:
    return HmacKeyring(KEY)


def test_leaf_shapes(ring: HmacKeyring) -> None:
    leaves = leaves_from_verification(_plan(), _report(), ring)
    assert len(leaves) == 2 + 1 + 1 + 1
    by = {(lf.table, lf.key_hmac): lf for lf in leaves}
    for k in (100, 101):
        lf = by[("app.purchase_order", ring.key_digest(RecordKey(ORDER, ("id",), (k,))))]
        assert (lf.action, lf.check_types, lf.passed) == (
            PlanAction.EXPECT_CASCADE,
            (CheckType.CASCADE_COMPLETE,),
            True,
        )
    cust = by[("app.customer", ring.key_digest(RecordKey(CUSTOMER, ("id",), ("c-4242",))))]
    assert cust.passed and cust.check_types == (CheckType.ROW_ABSENT,)
    ltv = by[("app.customer_ltv", ring.digest("app.customer_ltv"))]
    assert ltv.check_types == (CheckType.DERIVED_CONSISTENT, CheckType.ROW_ABSENT)
    assert ltv.passed
    ext = by[("app.crm_export", ring.digest("app.crm_export"))]
    assert ext.action is PlanAction.UNRESOLVED and ext.check_types == () and not ext.passed


def test_leaves_sorted_and_no_raw_keys(ring: HmacKeyring) -> None:
    leaves = leaves_from_verification(_plan(), _report(), ring)
    blobs = [canonical_json(lf.as_canonical()) for lf in leaves]
    assert blobs == sorted(blobs)
    text = b"".join(blobs).decode()
    assert "c-4242" not in text  # (ints could appear inside hex by chance; strings cannot)
    for lf in leaves:
        assert len(lf.key_hmac) == 64


def test_failed_check_marks_leaf(ring: HmacKeyring) -> None:
    leaves = leaves_from_verification(_plan(), _report(row_absent_ok=False), ring)
    assert [lf.passed for lf in leaves if lf.table == "app.customer"] == [False]
    assert all(lf.passed for lf in leaves if lf.table == "app.purchase_order")


def test_item_without_bound_checks_is_not_passed(ring: HmacKeyring) -> None:
    report = VerificationReport(VerificationStatus.PASSED, ())
    leaves = leaves_from_verification(_plan(), report, ring)
    assert not any(lf.passed for lf in leaves)


def test_leaves_are_key_dependent() -> None:
    a = leaves_from_verification(_plan(), _report(), HmacKeyring(KEY))
    b = leaves_from_verification(_plan(), _report(), HmacKeyring(b"\x01" * 32))
    assert {lf.key_hmac for lf in a}.isdisjoint({lf.key_hmac for lf in b})


def test_round_trip_through_evidence_and_proofs(ring: HmacKeyring) -> None:
    plan, report = _plan(), _report()
    report = VerificationReport(VerificationStatus.PASSED, report.checks)
    leaves = leaves_from_verification(plan, report, ring)
    signer = Signer.generate()
    eb = build_evidence(
        plan,
        ExecutionEvidence(
            UUID(int=3),
            datetime(2026, 11, 4, 12, tzinfo=UTC),
            ExecutionResult(
                ExecutionMode.LIVE,
                ExecutionStatus.COMMITTED,
                (StepResult(1, StepStatus.APPLIED, 2, 2, 1),),
            ),
        ),
        report,
        leaves,
        ChainAnchor(5, "d" * 64),
        signer,
        datetime(2026, 11, 4, 12, 1, tzinfo=UTC),
    )
    assert list(eb.leaves) == leaves  # already in bundle order
    r = verify_evidence_offline(eb.export_json(), signer.public_key_pem, eb.leaves_canonical)
    assert r.valid and r.merkle_ok, r.errors
    # an auditor proves "customer c-4242 was erased" from its HMAC alone
    tree = MerkleTree.build(leaves)
    target = ring.key_digest(RecordKey(CUSTOMER, ("id",), ("c-4242",)))
    idx = next(i for i, lf in enumerate(leaves) if lf.key_hmac == target)
    assert verify_proof(leaves[idx], tree.proof(idx), eb.merkle_root)
