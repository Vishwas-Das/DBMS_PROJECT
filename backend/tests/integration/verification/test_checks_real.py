"""Per-item verification checks against REAL PostgreSQL (seeded tiny/7, rolled back).

Each test builds contract plan items by hand, applies (or deliberately does not apply) the DML
with ``PgTargetWriter`` and calls ``verify_execution`` with the real ``PgTargetReader``. Every
check must fail before the change and pass after it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
from typing import Any

import pytest
from sqlalchemy import text

from lae.contracts.enums import CheckType, LineageLevel, PlanAction
from lae.contracts.types import CheckResult, TableRef, VerificationReport
from lae.verification import PSEUDONYM_PATTERN, is_anonymized

pytestmark = pytest.mark.integration

CT = CheckType
A = PlanAction


def t(name: str) -> TableRef:
    return TableRef("app", name)


def checks(r: VerificationReport, ct: CheckType, step: int | None = None) -> list[CheckResult]:
    return [c for c in r.checks if c.check_type is ct and (step is None or c.step_order == step)]


def one(r: VerificationReport, ct: CheckType, step: int | None = None) -> CheckResult:
    found = checks(r, ct, step)
    assert len(found) == 1, (ct, step, found)
    return found[0]


def assert_only_counts(r: VerificationReport) -> None:
    """Per-item checks carry counts only (never key or column values)."""
    for c in r.checks:
        if c.step_order is None:
            continue
        for v in (*c.observed.values(), *c.expected.values()):
            assert v is None or isinstance(v, bool | int), (c.check_type, v)


def subject_orders(w: Any) -> tuple[tuple[Any, ...], ...]:
    keys: tuple[tuple[Any, ...], ...] = w.keys(
        "SELECT id FROM app.purchase_order WHERE customer_id = :c", c=w.subject
    )
    return keys


# ------------------------------------------------------------------ ROW_ABSENT


def test_row_absent_real(world: Any) -> None:
    w = world
    keys = w.keys("SELECT customer_id FROM app.loyalty_account WHERE customer_id = :c", c=w.subject)
    assert keys == ((w.subject,),)
    plan = w.plan([w.item(1, A.DELETE_ROWS, t("loyalty_account"), ("customer_id",), keys)])

    before = one(w.verify(plan), CT.ROW_ABSENT, 1)
    assert not before.passed and dict(before.observed) == {"remaining_rows": 1}

    w.apply(plan)
    after = w.verify(plan)
    c = one(after, CT.ROW_ABSENT, 1)
    assert c.passed and dict(c.observed) == {"remaining_rows": 0}
    assert dict(c.expected) == {"remaining_rows": 0, "planned_keys": 1}
    assert_only_counts(after)


def test_row_absent_with_composite_keys(world: Any) -> None:
    """Composite key (order_id, line_no) through the real IN (VALUES ...) predicate."""
    w = world
    lines = w.keys(
        "SELECT order_id, line_no FROM app.order_line ORDER BY order_id, line_no LIMIT 25"
    )
    assert len(lines) == 25
    plan = w.plan([w.item(1, A.DELETE_ROWS, t("order_line"), ("order_id", "line_no"), lines)])
    # shipments reference order lines (NO ACTION): remove them so the planned delete can run
    w.conn.execute(
        text(
            "DELETE FROM app.shipment s USING (SELECT order_id, line_no FROM app.order_line "
            "ORDER BY order_id, line_no LIMIT 25) l "
            "WHERE s.order_id = l.order_id AND s.line_no = l.line_no"
        )
    )
    assert one(w.verify(plan), CT.ROW_ABSENT, 1).observed["remaining_rows"] == 25
    w.apply(plan)
    assert one(w.verify(plan), CT.ROW_ABSENT, 1).passed


# ------------------------------------------------------------------ CASCADE_COMPLETE


def test_cascade_complete_real(world: Any) -> None:
    w = world
    plan = w.subject_plan()
    step = {(i.action, i.table): i for i in plan.items}
    customer = step[(A.DELETE_ROWS, t("customer"))]
    cascade = step[(A.EXPECT_CASCADE, t("customer_address"))]
    assert len(cascade.record_keys) >= 1

    w.apply(plan, skip=[customer.step_order])  # the parent row stays -> no cascade
    failing = one(w.verify(plan), CT.CASCADE_COMPLETE, cascade.step_order)
    assert not failing.passed
    assert failing.observed["remaining_rows"] == len(cascade.record_keys)
    assert not one(w.verify(plan), CT.ROW_ABSENT, customer.step_order).passed

    assert w.apply_item(customer) == 1  # ON DELETE CASCADE removes the addresses
    ok = one(w.verify(plan), CT.CASCADE_COMPLETE, cascade.step_order)
    assert ok.passed and ok.observed["remaining_rows"] == 0


# ------------------------------------------------------------------ COLUMNS_ANONYMIZED


def test_columns_anonymized_with_real_pseudonyms(world: Any) -> None:
    w = world
    orders = subject_orders(w)
    assert len(orders) >= 2
    item = w.item(
        1, A.ANONYMIZE_ROWS, t("purchase_order"), ("id",), orders,
        ("shipping_address", "shipping_name"),
    )  # fmt: skip
    w.assignments[1] = {"shipping_name": w.PSEUDO, "shipping_address": w.NULL}
    plan = w.plan([item])

    for c in checks(w.verify(plan), CT.COLUMNS_ANONYMIZED, 1):
        assert not c.passed
        assert dict(c.observed) == {"rows_present": len(orders), "violations": len(orders)}

    # anonymize all but one order: exactly one violation per column remains
    partial = dataclasses.replace(item, record_keys=orders[:-1], expected_row_count=len(orders) - 1)
    assert w.apply_item(partial) == len(orders) - 1
    for c in checks(w.verify(plan), CT.COLUMNS_ANONYMIZED, 1):
        assert not c.passed and dict(c.observed) == {"rows_present": len(orders), "violations": 1}

    w.apply(plan)
    report = w.verify(plan)
    cols = checks(report, CT.COLUMNS_ANONYMIZED, 1)
    assert [c.column for c in cols] == ["shipping_address", "shipping_name"]
    assert all(c.passed for c in cols)
    assert_only_counts(report)

    # Stored pseudonyms: 'anon_' || left(hex(HMAC-SHA256(key, pk::text)), 16) -- recomputed in
    # Python, matched by the verifier's pattern and by the SQL regex ^anon_[0-9a-f]{16}$.
    rows = w.q(
        "SELECT id, shipping_name, shipping_address, shipping_name ~ '^anon_[0-9a-f]{16}$' "
        "FROM app.purchase_order WHERE id = ANY(:ids) ORDER BY id",
        ids=[k[0] for k in orders],
    )
    for oid, name, address, sql_match in rows:
        want = "anon_" + hmac.new(w.HMAC_KEY, str(oid).encode(), hashlib.sha256).hexdigest()[:16]
        assert name == want and sql_match is True
        assert PSEUDONYM_PATTERN.fullmatch(name) and is_anonymized(name)
        assert address is None


def test_columns_anonymized_fails_when_retained_row_disappears(world: Any) -> None:
    w = world
    orders = subject_orders(w)
    w.assignments[1] = {"shipping_name": w.PSEUDO}
    plan = w.plan(
        [w.item(1, A.ANONYMIZE_ROWS, t("purchase_order"), ("id",), orders, ("shipping_name",))]
    )
    w.apply(plan)
    # a retained (tax) order vanishes: dependents first, then the order itself
    victim = orders[0][0]
    w.conn.execute(text("DELETE FROM app.shipment WHERE order_id = :o"), {"o": victim})
    w.conn.execute(text("DELETE FROM app.payment WHERE order_id = :o"), {"o": victim})
    w.conn.execute(text("DELETE FROM app.purchase_order WHERE id = :o"), {"o": victim})
    c = one(w.verify(plan), CT.COLUMNS_ANONYMIZED, 1)
    assert not c.passed
    assert dict(c.observed) == {"rows_present": len(orders) - 1, "violations": 0}


# ------------------------------------------------------------------ REFERENCE_NULLIFIED


def test_reference_nullified_real(world: Any) -> None:
    w = world
    referring = w.keys("SELECT id FROM app.customer WHERE referrer_id = :c", c=w.subject)
    expected = tuple((c,) for c in w.truth.meta["known_subjects"]["referred_by_primary"])
    assert referring == expected
    plan = w.plan(
        [w.item(1, A.NULLIFY_REFERENCE, t("customer"), ("id",), referring, ("referrer_id",))]
    )
    before = one(w.verify(plan), CT.REFERENCE_NULLIFIED, 1)
    assert not before.passed and before.observed["violations"] == len(referring)

    w.apply(plan)
    after = one(w.verify(plan), CT.REFERENCE_NULLIFIED, 1)
    assert after.passed
    assert dict(after.observed) == {
        "rows_present": len(referring),
        "rows_deleted_by_plan": 0,
        "rows_missing": 0,
        "violations": 0,
    }
    # the other customers themselves are never deleted
    assert w.count("SELECT count(*) FROM app.customer WHERE id = ANY(:i)",
                   i=[k[0] for k in referring]) == len(referring)  # fmt: skip


def test_reference_nullified_missing_row_unless_plan_deletes_it(world: Any) -> None:
    """A row the plan only nullifies must survive; it may vanish only via a DELETE item."""
    w = world
    tickets = w.keys("SELECT id FROM app.support_ticket WHERE customer_id = :c", c=w.subject)
    assert len(tickets) >= 2
    nullify = w.item(1, A.NULLIFY_REFERENCE, t("support_ticket"), ("id",), tickets,
                     ("customer_id",))  # fmt: skip
    plan = w.plan([nullify])
    w.apply(plan)
    victim = tickets[0]
    # ticket_message rows CASCADE with the ticket (raw test DML, outside the plan)
    w.conn.execute(text("DELETE FROM app.support_ticket WHERE id = :v"), {"v": victim[0]})
    c = one(w.verify(plan), CT.REFERENCE_NULLIFIED, 1)
    assert not c.passed
    assert dict(c.observed) == {
        "rows_present": len(tickets) - 1,
        "rows_deleted_by_plan": 0,
        "rows_missing": 1,
        "violations": 0,
    }

    explained = w.plan([nullify, w.item(2, A.DELETE_ROWS, t("support_ticket"), ("id",), (victim,))])
    c2 = one(w.verify(explained), CT.REFERENCE_NULLIFIED, 1)
    assert c2.passed and c2.observed["rows_deleted_by_plan"] == 1
    assert c2.observed["rows_missing"] == 0


# ------------------------------------------------------------------ RETENTION_HONORED


def test_retention_honored_real(world: Any) -> None:
    w = world
    payments = w.keys(
        "SELECT p.id FROM app.payment p JOIN app.purchase_order o ON o.id = p.order_id "
        "WHERE o.customer_id = :c",
        c=w.subject,
    )
    truth_payments = sorted(
        tuple(k) for tb, k, act in w.truth.subject(w.subject)["retained"] if tb == "app.payment"
    )
    assert list(payments) == truth_payments and payments
    plan = w.plan([w.item(1, A.RETAIN_ROWS, t("payment"), ("id",), payments)])

    ok = one(w.verify(plan), CT.RETENTION_HONORED, 1)
    assert ok.passed and dict(ok.observed) == {"rows_present": len(payments)}

    w.conn.execute(text("DELETE FROM app.payment WHERE id = :p"), {"p": payments[0][0]})
    bad = one(w.verify(plan), CT.RETENTION_HONORED, 1)
    assert not bad.passed and dict(bad.observed) == {"rows_present": len(payments) - 1}


# ------------------------------------------------------------------ DERIVED_CONSISTENT


def test_derived_consistent_matview_refresh_real(world: Any) -> None:
    """Delete the subject's payments: customer_ltv is stale until REFRESH MATERIALIZED VIEW."""
    w = world
    payments = w.keys(
        "SELECT p.id FROM app.payment p JOIN app.purchase_order o ON o.id = p.order_id "
        "WHERE o.customer_id = :c",
        c=w.subject,
    )
    assert payments
    assert w.count("SELECT count(*) FROM app.customer_ltv WHERE customer_id = :c", c=w.subject) == 1
    plan = w.plan(
        [
            w.item(1, A.DELETE_ROWS, t("payment"), ("id",), payments),
            w.item(2, A.REFRESH_MATVIEW, t("customer_ltv"), ("customer_id",), ((w.subject,),)),
            w.item(3, A.RECOMPUTE_TABLE, t("customer_segment"), ("customer_id",), ((w.subject,),)),
            w.item(4, A.REFRESH_MATVIEW, t("daily_revenue"), level=LineageLevel.TABLE),
        ]
    )
    w.apply_item(plan.items[0])  # payments gone; derived artifacts not yet refreshed

    stale = w.verify(plan)
    ltv = one(stale, CT.DERIVED_CONSISTENT, 2)
    assert not ltv.passed and ltv.observed["difference_rows"] == 1  # stored row has no fresh twin
    assert not one(stale, CT.ROW_ABSENT, 2).passed
    assert one(stale, CT.DERIVED_CONSISTENT, 3).passed  # segment matches the (stale) matview
    assert not one(stale, CT.ROW_ABSENT, 3).passed
    rev = one(stale, CT.DERIVED_CONSISTENT, 4)
    assert not rev.passed and rev.observed["difference_rows"] >= 2  # changed days: old+new row

    w.apply_item(plan.items[1])  # REFRESH MATERIALIZED VIEW app.customer_ltv
    mid = w.verify(plan)
    assert one(mid, CT.DERIVED_CONSISTENT, 2).passed and one(mid, CT.ROW_ABSENT, 2).passed
    seg = one(mid, CT.DERIVED_CONSISTENT, 3)
    assert not seg.passed and seg.observed["difference_rows"] == 1  # artifact-on-artifact

    w.apply(plan, skip=[1, 2])  # recompute segment, refresh daily_revenue
    done = w.verify(plan)
    for step in (2, 3, 4):
        assert one(done, CT.DERIVED_CONSISTENT, step).passed
    assert one(done, CT.ROW_ABSENT, 3).passed
    assert [c for c in done.checks if not c.passed] == []
    assert_only_counts(done)


# ------------------------------------------------------------------ EMBEDDING_ABSENT


def test_embedding_absent_real(world: Any) -> None:
    """Embeddings have no FK: deleting the documents leaves them behind until deleted by key."""
    w = world
    docs = w.keys("SELECT id FROM app.document WHERE customer_id = :c", c=w.subject)
    embeddings = w.keys(
        "SELECT id FROM app.document_embedding WHERE source_document_id = ANY(:d)",
        d=[k[0] for k in docs],
    )
    truth = sorted(
        tuple(k) for tb, k in w.truth.subject(w.subject)["derived_rows"]
        if tb == "app.document_embedding"
    )  # fmt: skip
    assert list(embeddings) == truth and embeddings

    only_docs = w.plan([w.item(1, A.DELETE_ROWS, t("document"), ("id",), docs)])
    w.apply(only_docs)
    r = w.verify(only_docs)
    emb = one(r, CT.EMBEDDING_ABSENT)
    assert not emb.passed and emb.step_order is None  # no plan item on the embedding table
    assert dict(emb.observed) == {"remaining_rows": len(embeddings)}
    assert dict(emb.expected) == {"remaining_rows": 0, "source_keys": len(docs)}
    assert one(r, CT.ROW_ABSENT, 1).passed  # the documents themselves are gone

    full = w.plan(
        [
            w.item(1, A.DELETE_DERIVED_BY_KEY, t("document_embedding"), ("id",), embeddings),
            w.item(2, A.DELETE_ROWS, t("document"), ("id",), docs),
        ]
    )
    w.apply_item(full.items[0])
    r2 = w.verify(full)
    emb2 = one(r2, CT.EMBEDDING_ABSENT)
    assert emb2.passed and emb2.step_order == 1 and emb2.observed["remaining_rows"] == 0
    assert r2.outcome.value == "VERIFIED" and r2.coverage == 1
