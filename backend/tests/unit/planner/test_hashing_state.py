"""Plan hash determinism and sensitivity (AT-12), status machine, conflict acknowledgement."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from erasure_harness import ack_all, make_plan, shop, uid

from lae.contracts.enums import ConflictCode, LineageLevel, PlanAction, PlanStatus, Severity
from lae.contracts.types import ErasurePlan, PlanConflict, TableRef
from lae.errors import InvalidStateTransition, PlanBlockedError, PlanError, PlanHashMismatch
from lae.planner import (
    ACKNOWLEDGEABLE_CODES,
    TRANSITIONS,
    acknowledge_conflict,
    check_approval,
    check_transition,
    compute_plan_hash,
    initial_status,
    plan_hash_payload,
    plan_with_hash,
)

pytestmark = pytest.mark.unit

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[2] / "src"


def _plan() -> ErasurePlan:
    sh = shop()
    return make_plan(sh.schema, sh.db)


# ---------------------------------------------------------------------- determinism


def test_same_inputs_same_hash_and_bytes() -> None:
    a, b = _plan(), _plan()
    assert a == b
    assert a.plan_hash == b.plan_hash == compute_plan_hash(a)


def test_hash_is_stable_across_processes() -> None:
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "from erasure_harness import shop, make_plan;"
        "sh = shop(); print(make_plan(sh.schema, sh.db).plan_hash)"
    )
    env = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONHASHSEED": "12345"}
    out = subprocess.run(  # noqa: S603 - fixed interpreter and arguments
        [sys.executable, "-c", code, str(HERE)], capture_output=True, text=True, env=env, check=True
    )
    assert out.stdout.strip() == _plan().plan_hash


def test_plan_with_hash_is_idempotent_and_ignores_plan_id() -> None:
    p = _plan()
    assert plan_with_hash(p) == p
    other = replace(p, plan_id=UUID(int=99), plan_hash="x")
    assert compute_plan_hash(other) == p.plan_hash
    assert "plan_id" not in plan_hash_payload(p)


def test_conflict_order_does_not_change_hash() -> None:
    p = _plan()
    rev = replace(p, conflicts=tuple(reversed(p.conflicts)))
    assert compute_plan_hash(rev) == p.plan_hash


# ---------------------------------------------------------------------- sensitivity


ITEM_MUTATIONS = {
    "step_order": lambda i: replace(i, step_order=i.step_order + 100),
    "action": lambda i: replace(i, action=PlanAction.MANUAL_REVIEW),
    "table": lambda i: replace(i, table=TableRef("app", "zzz")),
    "lineage_level": lambda i: replace(
        i, lineage_level=LineageLevel.TABLE, expected_row_count=None
    ),
    "key_columns": lambda i: replace(i, key_columns=("other",)),
    "record_keys": lambda i: replace(i, record_keys=((123456,),)),
    "target_columns": lambda i: replace(i, target_columns=("x",)),
    "expected_row_count": lambda i: replace(i, expected_row_count=(i.expected_row_count or 0) + 1),
    "depends_on": lambda i: replace(i, depends_on=(*i.depends_on, 1)),
    "reason_edge_ids": lambda i: replace(i, reason_edge_ids=(uid("x"),)),
    "reason": lambda i: replace(i, reason=i.reason + "!"),
}


@pytest.mark.parametrize("field_name", sorted(ITEM_MUTATIONS))
def test_any_item_field_change_changes_hash(field_name: str) -> None:
    p = _plan()
    idx = next(
        n for n, i in enumerate(p.items) if i.action is PlanAction.DELETE_ROWS and i.depends_on
    )
    items = list(p.items)
    items[idx] = ITEM_MUTATIONS[field_name](items[idx])
    assert compute_plan_hash(replace(p, items=tuple(items))) != p.plan_hash


@pytest.mark.parametrize(
    "change",
    [
        {"request_id": UUID(int=5)},
        {"schema_fingerprint": "0" * 64},
        {"record_set_hash": "1" * 64},
        {"items": ()},
    ],
)
def test_plan_level_changes_change_hash(change: dict[str, object]) -> None:
    p = _plan()
    assert compute_plan_hash(replace(p, **change)) != p.plan_hash  # type: ignore[arg-type]


def test_conflict_changes_change_hash() -> None:
    p = _plan()
    c = p.conflicts[0]
    for mutated in (
        replace(c, resolved=True),
        replace(c, severity=Severity.WARNING),
        replace(c, code=ConflictCode.NOT_NULL_REFERENCE),
        replace(c, table=TableRef("app", "zzz")),
    ):
        assert compute_plan_hash(replace(p, conflicts=(mutated, *p.conflicts[1:]))) != p.plan_hash
    assert (
        compute_plan_hash(replace(p, conflicts=(replace(c, message="other"), *p.conflicts[1:])))
        == p.plan_hash
    )


# ---------------------------------------------------------------------- acknowledgement


def test_acknowledge_resolves_and_rehashes() -> None:
    p = _plan()
    idx = next(n for n, c in enumerate(p.conflicts) if c.code is ConflictCode.SHARED_RECORD)
    assert p.is_blocked
    q = acknowledge_conflict(p, idx)
    assert q.conflicts[idx].resolved and not q.is_blocked
    assert q.plan_hash != p.plan_hash and q.plan_hash == compute_plan_hash(q)
    assert q.items == p.items
    with pytest.raises(PlanError) as ei:
        acknowledge_conflict(q, idx)
    assert ei.value.code == "CONFLICT_ALREADY_RESOLVED"


def test_acknowledge_out_of_range() -> None:
    p = _plan()
    for bad in (-1, len(p.conflicts)):
        with pytest.raises(PlanError) as ei:
            acknowledge_conflict(p, bad)
        assert ei.value.code == "CONFLICT_NOT_FOUND"


@pytest.mark.parametrize("code", sorted(ConflictCode))
def test_only_listed_codes_are_acknowledgeable(code: ConflictCode) -> None:
    p = replace(
        _plan(), conflicts=(PlanConflict(code, Severity.BLOCKING, TableRef("app", "t"), "m"),)
    )
    if code in ACKNOWLEDGEABLE_CODES:
        assert acknowledge_conflict(p, 0).conflicts[0].resolved
    else:
        with pytest.raises(PlanError) as ei:
            acknowledge_conflict(p, 0)
        assert ei.value.code == "CONFLICT_NOT_ACKNOWLEDGEABLE"
    assert {
        ConflictCode.OPAQUE_ARTIFACT,
        ConflictCode.SHARED_RECORD,
        ConflictCode.RETAINED_WITH_PII,
        ConflictCode.EXTERNAL_ARTIFACT,
        ConflictCode.NO_PRIMARY_KEY,
    } == ACKNOWLEDGEABLE_CODES


def test_ack_all_helper_unblocks_shop() -> None:
    p = ack_all(_plan())
    assert not p.is_blocked and all(c.resolved for c in p.conflicts)


# ---------------------------------------------------------------------- status machine


ALLOWED = {
    (PlanStatus.DRAFT, PlanStatus.BLOCKED),
    (PlanStatus.DRAFT, PlanStatus.APPROVED),
    (PlanStatus.DRAFT, PlanStatus.SUPERSEDED),
    (PlanStatus.BLOCKED, PlanStatus.DRAFT),
    (PlanStatus.BLOCKED, PlanStatus.SUPERSEDED),
    (PlanStatus.APPROVED, PlanStatus.EXECUTED),
    (PlanStatus.APPROVED, PlanStatus.FAILED),
    (PlanStatus.APPROVED, PlanStatus.SUPERSEDED),
    (PlanStatus.EXECUTED, PlanStatus.VERIFIED),
    (PlanStatus.EXECUTED, PlanStatus.FAILED),
}


@pytest.mark.parametrize("current", list(PlanStatus))
@pytest.mark.parametrize("target", list(PlanStatus))
def test_transition_matrix(current: PlanStatus, target: PlanStatus) -> None:
    if (current, target) in ALLOWED:
        check_transition(current, target)
    else:
        with pytest.raises(InvalidStateTransition) as ei:
            check_transition(current, target)
        assert ei.value.details == {"from": current.value, "to": target.value}
    assert set(TRANSITIONS) == set(PlanStatus)


def test_initial_status_and_approval_checks() -> None:
    p = _plan()
    assert initial_status(p) is PlanStatus.BLOCKED
    with pytest.raises(PlanBlockedError):
        check_approval(p, PlanStatus.BLOCKED, p.plan_hash)
    q = ack_all(p)
    assert initial_status(q) is PlanStatus.DRAFT
    check_approval(q, PlanStatus.DRAFT, q.plan_hash)
    with pytest.raises(PlanHashMismatch):
        check_approval(q, PlanStatus.DRAFT, p.plan_hash)
    with pytest.raises(InvalidStateTransition):
        check_approval(q, PlanStatus.EXECUTED, q.plan_hash)
