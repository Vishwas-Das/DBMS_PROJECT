"""Property tests over random schemas, policies and data (AT-10 ordering, safety, atomicity).

Each example builds a random schema of 1-4 tables below `customer` with random FKs
(CASCADE / RESTRICT / NO ACTION / SET NULL, nullable or not, plus optional back-references
that create table-level and record-level cycles), random retention policies and random rows
for three customers. The plan is executed on the FK-enforcing FakeDB, which acts as the oracle:
a wrong order raises a foreign-key violation.
"""

from __future__ import annotations

from typing import Any

import pytest
from erasure_harness import (
    FakeAudit,
    FakeDB,
    Schema,
    StepClock,
    T,
    ack_all,
    discover,
    make_plan,
    rediscover_fn,
    run_in_txn,
)
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from lae.contracts.enums import (
    ConflictCode,
    ExecutionMode,
    ExecutionStatus,
    OnDeleteAction,
    PlanAction,
    RecordRole,
    RetentionAction,
)
from lae.contracts.types import Approval, ErasurePlan
from lae.errors import PlanBlockedError
from lae.executor import execute_plan
from lae.planner import pseudonym_columns

pytestmark = pytest.mark.unit

ON_DELETE = [
    OnDeleteAction.CASCADE,
    OnDeleteAction.RESTRICT,
    OnDeleteAction.NO_ACTION,
    OnDeleteAction.SET_NULL,
]
POLICIES = [RetentionAction.DELETE] * 4 + [RetentionAction.ANONYMIZE, RetentionAction.RETAIN]
OWNED_ROLES = {RecordRole.SUBJECT_ROOT, RecordRole.OWNED}
BASE = {
    PlanAction.NULLIFY_REFERENCE,
    PlanAction.ANONYMIZE_ROWS,
    PlanAction.DELETE_ROWS,
    PlanAction.EXPECT_CASCADE,
}


@st.composite
def scenario(draw: st.DrawFn) -> tuple[Schema, FakeDB]:
    n = draw(st.integers(1, 4))
    names = [f"t{i}" for i in range(n)]
    # (child, column, parent, on_delete, nullable)
    specs: list[tuple[str, str, str, OnDeleteAction, bool]] = []
    for i, name in enumerate(names):
        parents = draw(
            st.lists(st.sampled_from(["customer", *names[:i]]), min_size=1, max_size=2, unique=True)
        )
        for p in parents:
            od = draw(st.sampled_from(ON_DELETE))
            nullable = od is OnDeleteAction.SET_NULL or draw(st.booleans())
            specs.append((name, f"{p}_id", p, od, nullable))
        if i > 0 and draw(st.booleans()):
            # nullable back-reference from the previous table: table-level cycle
            od = draw(st.sampled_from([OnDeleteAction.NO_ACTION, OnDeleteAction.SET_NULL]))
            specs.append((names[i - 1], f"back_{name}_id", name, od, True))

    s = Schema()
    s.table("customer", "id", "referrer_id", "email", not_null=("email",))
    s.fk(
        "fk_ref",
        "customer",
        ["referrer_id"],
        "customer",
        ["id"],
        draw(st.sampled_from([OnDeleteAction.SET_NULL, OnDeleteAction.NO_ACTION])),
    )
    s.classify("customer", "email")
    for name in names:
        mine = [sp for sp in specs if sp[0] == name]
        s.table(
            name,
            "id",
            "secret",
            *[sp[1] for sp in mine],
            not_null=[sp[1] for sp in mine if not sp[4]],
        )
        s.classify(name, "secret")
        s.policy(name, draw(st.sampled_from(POLICIES)))
    for child, col, parent, od, _ in specs:
        s.fk(f"fk_{child}_{col}", child, [col], parent, ["id"], od)

    db = FakeDB(s)
    db.insert("customer", id=1, referrer_id=None, email="subject@example.com")
    db.insert("customer", id=2, referrer_id=draw(st.sampled_from([None, 1])), email="b@example.com")
    db.insert("customer", id=3, referrer_id=None, email="c@example.com")
    next_id = 100
    for i, name in enumerate(names):
        for _ in range(draw(st.integers(0, 4))):
            row: dict[str, Any] = {"id": next_id, "secret": f"s{next_id}"}
            next_id += 1
            ok = True
            for child, col, parent, _od, nullable in specs:
                if child != name or parent not in ("customer", *names[:i]):
                    continue  # back-references are filled in afterwards
                choices: list[Any] = [r["id"] for r in db.rows(parent)] + (
                    [None] if nullable else []
                )
                if not choices:
                    ok = False
                    break
                row[col] = draw(st.sampled_from(choices))
            if ok:
                db.insert(name, **row)
    for child, col, parent, _od, _n in specs:
        if col.startswith("back_"):
            targets = [r["id"] for r in db.rows(parent)] + [None]
            for r in db.rows(child):
                r[col] = draw(st.sampled_from(targets))
    return s, db


def _step_of(plan: ErasurePlan) -> dict[Any, int]:
    out: dict[Any, int] = {}
    for i in plan.items:
        if i.action is PlanAction.DELETE_ROWS:
            for k in i.record_keys:
                out[(i.table, k)] = i.step_order
    return out


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(scenario())
def test_random_plans_are_ordered_and_execute_safely(case: tuple[Schema, FakeDB]) -> None:
    s, db = case
    disc = discover(s, db, "customer", (1,))
    plan = make_plan(s, db, subject=(1,))
    assert plan == make_plan(s, db, subject=(1,))  # deterministic

    # AT-10: explicit child deletes precede parent deletes for restricting, unbroken FKs
    lg = s.lineage()
    step = _step_of(plan)
    nullified = {
        (i.table, k, c)
        for i in plan.items
        if i.action in (PlanAction.NULLIFY_REFERENCE, PlanAction.ANONYMIZE_ROWS)
        for k in i.record_keys
        for c in i.target_columns
    }
    cyclic = ConflictCode.UNBREAKABLE_CYCLE in {c.code for c in plan.conflicts}
    for p, c, eid in disc.record_graph.edges() if not cyclic else []:
        e = lg.edge(eid)
        if e.on_delete not in (OnDeleteAction.RESTRICT, OnDeleteAction.NO_ACTION):
            continue
        sp, sc = step.get((p.table, p.values)), step.get((c.table, c.values))
        if sp is None or sc is None or p.table == c.table:
            continue
        if any((c.table, c.values, col) in nullified for col in e.target_columns):
            continue
        assert sc < sp, (c, p)
    # derived steps after base steps; no-DML steps last
    base = [i.step_order for i in plan.items if i.action in BASE]
    last = [
        i.step_order
        for i in plan.items
        if i.action in (PlanAction.RETAIN_ROWS, PlanAction.MANUAL_REVIEW)
    ]
    if base and last:
        assert max(base) < min(last)
    for i in plan.items:
        assert all(d < i.step_order for d in i.depends_on)

    plan = ack_all(plan)
    approval = Approval(plan.plan_id, plan.plan_hash, "reviewer", StepClock()())
    audit = FakeAudit()
    rediscover = rediscover_fn(s, "customer", (1,))
    pseudo = pseudonym_columns(plan, s.policies(), lg)

    def go(mode: ExecutionMode) -> Any:
        return execute_plan(
            plan, approval, db, rediscover, audit, mode, clock=StepClock(), pseudonym_columns=pseudo
        )

    event(f"items={min(len(plan.items), 9)}")
    if plan.is_blocked:
        event(
            "blocked: " + ",".join(sorted({c.code.value for c in plan.conflicts if not c.resolved}))
        )
        with pytest.raises(PlanBlockedError):
            go(ExecutionMode.LIVE)
        return
    if ConflictCode.SHARED_RECORD in {c.code for c in plan.conflicts}:
        event("executed with detached shared rows")

    event("executed")
    if any(i.action is PlanAction.NULLIFY_REFERENCE and "cycle" in i.reason for i in plan.items):
        event("executed with cycle break")
    if len({i.table for i in plan.items if i.action is PlanAction.DELETE_ROWS}) < len(
        [i for i in plan.items if i.action is PlanAction.DELETE_ROWS]
    ):
        event("executed with ranked batches")
    before = db.checksum()
    others_before = {r["id"]: dict(r) for r in db.rows("customer") if r["id"] != 1}
    graph_rows = {(k.table, k.values) for k, _ in disc.record_graph.nodes()}
    not_owned = sorted(
        (k for k, a in disc.record_graph.nodes() if a.role not in OWNED_ROLES),
        key=lambda k: k.sort_key(),
    )
    untouched_before = _rows_outside(db, s, graph_rows)

    dry = run_in_txn(db, ExecutionMode.DRY_RUN, lambda: go(ExecutionMode.DRY_RUN))
    assert db.checksum() == before and dry.status is ExecutionStatus.ROLLED_BACK
    live = run_in_txn(db, ExecutionMode.LIVE, lambda: go(ExecutionMode.LIVE))
    assert live.status is ExecutionStatus.COMMITTED and live.steps == dry.steps

    # subject rows planned for removal are gone; retained rows survive
    for i in plan.items:
        present = {tuple(r[c] for c in i.key_columns) for r in db.rows(i.table.name)}
        if i.action in (PlanAction.DELETE_ROWS, PlanAction.EXPECT_CASCADE):
            assert not present & set(i.record_keys)
        if i.action in (PlanAction.RETAIN_ROWS, PlanAction.ANONYMIZE_ROWS):
            assert set(i.record_keys) <= present
    # other customers keep every column except a reference to the subject
    after = {r["id"]: r for r in db.rows("customer")}
    assert set(after) == set(others_before)
    for cid, row in others_before.items():
        expect = dict(row)
        if expect["referrer_id"] == 1:
            expect["referrer_id"] = None
        assert after[cid] == expect
    # rows unreachable from the subject are byte-identical
    assert _rows_outside(db, s, graph_rows) == untouched_before
    # policy §2: no row that is not OWNED by the subject is ever deleted
    for k in not_owned:
        present = {tuple(r[c] for c in k.columns) for r in db.rows(k.table.name)}
        assert k.values in present, k


def _rows_outside(db: FakeDB, s: Schema, graph_rows: set[Any]) -> list[Any]:
    out = []
    for t, info in sorted(s.tables.items()):
        if t == T("customer"):
            continue
        for r in db.rows(t.name):
            key = tuple(r[c] for c in info.primary_key)
            if (t, key) not in graph_rows:
                out.append((t.name, sorted(r.items(), key=lambda kv: kv[0])))
    return sorted(out, key=repr)
