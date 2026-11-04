"""PgTargetReader / PgTargetWriter against the seeded demo schema (rolled back)."""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, text

from lae.audit.hmac_keys import HmacKeyring
from lae.contracts.enums import Normalization
from lae.contracts.ports import TargetReader, TargetWriter
from lae.contracts.types import CatalogSnapshot, ColumnAssignment, ColumnRef, TableRef
from lae.db.catalog_queries import PgCatalogReader
from lae.db.target import PgTargetReader, PgTargetWriter
from lae.errors import (
    ConfigurationError,
    TargetQueryError,
    UnknownColumnError,
    UnknownTableError,
    ValidationError,
)

pytestmark = pytest.mark.integration

KEY = b"k" * 32
FP = "0" * 64
NULL = ColumnAssignment("NULL")
PSEUDO = ColumnAssignment("PSEUDONYM")


def tref(name: str) -> TableRef:
    return TableRef("app", name)


@pytest.fixture
def catalog(app_conn: Connection) -> CatalogSnapshot:
    tables = PgCatalogReader(app_conn).list_tables(["app"])
    return CatalogSnapshot({t.ref: t for t in tables}, ("app",), FP)


@pytest.fixture
def truth(seeded_db: tuple[Engine, Any]) -> Any:
    return seeded_db[1]


@pytest.fixture
def reader(app_conn: Connection, catalog: CatalogSnapshot) -> PgTargetReader:
    return PgTargetReader(app_conn, catalog)


@pytest.fixture
def writer(app_conn: Connection, catalog: CatalogSnapshot) -> PgTargetWriter:
    return PgTargetWriter(app_conn, catalog, hmac_key=KEY)


def owned(truth: Any, cid: int, table: str) -> list[tuple[Any, ...]]:
    return sorted(tuple(k) for t, k in truth.subject(cid)["owned"] if t == f"app.{table}")


def primary(truth: Any) -> int:
    return int(truth.meta["known_subjects"]["primary"])


def test_ports_implemented(reader: PgTargetReader, writer: PgTargetWriter) -> None:
    assert isinstance(reader, TargetReader)
    assert isinstance(writer, TargetWriter)


# ------------------------------------------------------------------ reads


def test_select_by_columns_matches_ground_truth(reader: PgTargetReader, truth: Any) -> None:
    p = primary(truth)
    rows = reader.select_by_columns(tref("purchase_order"), ["customer_id"], [(p,)], ["id"])
    assert [(r["id"],) for r in rows] == owned(truth, p, "purchase_order")
    orders = [(r["id"],) for r in rows]
    lines = reader.select_by_columns(
        tref("order_line"), ["order_id"], orders, ["order_id", "line_no"]
    )
    assert [(r["order_id"], r["line_no"]) for r in lines] == owned(truth, p, "order_line")
    ships = reader.select_by_columns(
        tref("shipment"),
        ["order_id", "line_no"],
        [(r["order_id"], r["line_no"]) for r in lines],
        ["id"],
    )
    assert [(r["id"],) for r in ships] == owned(truth, p, "shipment")
    assert reader.count_by_columns(tref("order_line"), ["order_id"], orders) == len(lines)


def test_batching_and_dedup_give_identical_results(
    app_conn: Connection, catalog: CatalogSnapshot
) -> None:
    keys = [(i,) for i in range(20, 0, -1)] + [(3,), (3,)]
    big = PgTargetReader(app_conn, catalog)
    small = PgTargetReader(app_conn, catalog, batch_size=3)
    a = big.select_by_columns(tref("customer"), ["id"], keys, ["id", "email"])
    b = small.select_by_columns(tref("customer"), ["id"], keys, ["id", "email"])
    assert a == b and [r["id"] for r in a] == list(range(1, 21))
    assert small.count_by_columns(tref("customer"), ["id"], keys) == 20
    assert big.select_by_columns(tref("customer"), ["id"], [], ["id"]) == []
    with pytest.raises(ConfigurationError):
        PgTargetReader(app_conn, catalog, batch_size=1001)


def test_no_pk_table_orders_by_projection(reader: PgTargetReader, truth: Any) -> None:
    email = reader.fetch_columns(tref("customer"), ["id"], (primary(truth),), ["email"])
    assert email is not None
    rows = reader.select_by_columns(
        tref("marketing_event_log"), ["email"], [(email["email"],)], ["email", "event"]
    )
    assert len(rows) == 3
    assert rows == sorted(rows, key=lambda r: (r["email"], r["event"]))


def test_fetch_columns(reader: PgTargetReader) -> None:
    assert reader.fetch_columns(tref("customer"), ["id"], (10**9,), ["email"]) is None
    row = reader.fetch_columns(tref("order_line"), ["order_id", "line_no"], (1, 1), ["quantity"])
    assert row is not None and set(row) == {"quantity"}
    with pytest.raises(TargetQueryError):
        reader.fetch_columns(tref("order_line"), ["order_id"], (1,), ["line_no"])  # 2+ lines


def test_values_are_bound_never_interpolated(reader: PgTargetReader) -> None:
    evil = "x' OR '1'='1"
    assert reader.select_by_columns(tref("customer"), ["email"], [(evil,)], ["id"]) == []
    assert (
        reader.count_by_columns(tref("customer"), ["email"], [("'; DELETE FROM app.customer; --",)])
        == 0
    )
    assert reader.count_by_columns(tref("customer"), ["id"], [(1,)]) == 1


@pytest.mark.parametrize(
    "bad_table",
    [
        'customer"; DROP TABLE app.customer; --',
        "customer; DROP TABLE app.customer",
        "nope",
    ],
)
def test_injection_shaped_tables_rejected(reader: PgTargetReader, bad_table: str) -> None:
    with pytest.raises(UnknownTableError):
        reader.select_by_columns(tref(bad_table), ["id"], [(1,)], ["id"])


@pytest.mark.parametrize(
    "bad_column", ['id" OR 1=1 --', "id) OR (1=1", "email; DROP TABLE x", "nope", ""]
)
def test_injection_shaped_columns_rejected(
    reader: PgTargetReader, writer: PgTargetWriter, bad_column: str
) -> None:
    with pytest.raises(UnknownColumnError):
        reader.select_by_columns(tref("customer"), [bad_column], [(1,)], ["id"])
    with pytest.raises(UnknownColumnError):
        reader.select_by_columns(tref("customer"), ["id"], [(1,)], [bad_column])
    with pytest.raises(UnknownColumnError):
        writer.update_columns(tref("customer"), ["id"], [(1,)], {bad_column: NULL})


def test_engine_schema_unreachable(reader: PgTargetReader, writer: PgTargetWriter) -> None:
    with pytest.raises(UnknownTableError):
        reader.select_by_columns(TableRef("lae", "audit_event"), ["seq"], [(1,)], ["seq"])
    with pytest.raises(UnknownTableError):
        writer.delete_rows(TableRef("lae", "audit_event"), ["seq"], [(1,)])


def test_argument_validation(reader: PgTargetReader) -> None:
    with pytest.raises(ValidationError):
        reader.select_by_columns(tref("customer"), ["id"], [(1, 2)], ["id"])
    with pytest.raises(ValidationError):
        reader.select_by_columns(tref("customer"), [], [(1,)], ["id"])
    with pytest.raises(ValidationError):
        reader.select_by_columns(tref("customer"), ["id"], [(1,)], [])
    with pytest.raises(ValidationError):
        reader.select_by_columns(tref("customer"), "id", [(1,)], ["id"])
    with pytest.raises(ValidationError):
        reader.select_by_columns(tref("customer"), ["id"], [(True,)], ["id"])


def test_run_readonly_sql(app_conn: Connection, catalog: CatalogSnapshot) -> None:
    allowed = "SELECT count(*) FROM app.customer WHERE id <= %(n)s"
    r = PgTargetReader(app_conn, catalog, allowed_sql=[allowed + ";"])
    assert r.run_readonly_sql(allowed, {"n": 5}) == [(5,)]
    with pytest.raises(ValidationError):
        r.run_readonly_sql("SELECT 1", {})
    unrestricted = PgTargetReader(app_conn, catalog)
    # security review M1: non-SELECT text is now rejected before it reaches PostgreSQL
    with pytest.raises(ValidationError):
        unrestricted.run_readonly_sql("DELETE FROM app.marketing_event_log", {})
    # the READ ONLY savepoint is still the second line of defence (a SELECT calling a writer)
    with pytest.raises(TargetQueryError) as info:
        unrestricted.run_readonly_sql("SELECT nextval('app.customer_id_seq')", {})
    assert info.value.details["sqlstate"] == "25006"  # read_only_sql_transaction
    # the surrounding transaction is still healthy and read-write
    assert app_conn.execute(text("SELECT count(*) FROM app.marketing_event_log")).scalar_one() == 40
    app_conn.execute(text("DELETE FROM app.marketing_event_log WHERE event = 'LINK_CLICK'"))


# ------------------------------------------------------------------ HMAC residual matching


TRICKY = [
    "\tVivian.Vance@Example.TEST \x0b",
    "vivian.vance@example.test",  # same as above after LOWER_TRIM
    "ivian.vance@example.test",  # must NOT match vivian (btrim must not strip 'v')
    "victor@example.testv",
    "\x0b\x0bvera@example.test\x0b",
    "  MIXED.Case@Example.Test\n\r\f",
    "plain@example.test",
]


@pytest.fixture
def tricky_rows(app_conn: Connection) -> list[str]:
    for v in TRICKY:
        app_conn.execute(
            text(
                "INSERT INTO app.marketing_event_log (email, event, occurred_at) "
                "VALUES (:e, 'LINK_CLICK', '2025-01-01T00:00:00Z')"
            ),
            {"e": v},
        )
    return TRICKY


def test_lower_trim_matches_python_normalization(
    reader: PgTargetReader, tricky_rows: list[str]
) -> None:
    ring = HmacKeyring(KEY)
    col = ColumnRef(tref("marketing_event_log"), "email")
    expected_counts: dict[str, int] = {}
    for v in tricky_rows:
        d = ring.digest(v, Normalization.LOWER_TRIM)
        expected_counts[d] = expected_counts.get(d, 0) + 1
    for d, n in expected_counts.items():
        assert reader.hmac_match_count(col, [d], Normalization.LOWER_TRIM, KEY) == n
    vivian = ring.digest("vivian.vance@example.test", Normalization.LOWER_TRIM)
    ivian = ring.digest("ivian.vance@example.test", Normalization.LOWER_TRIM)
    assert expected_counts[vivian] == 2 and expected_counts[ivian] == 1
    victor = ring.digest("victor@example.testv", Normalization.LOWER_TRIM)
    assert reader.hmac_match_count(col, [victor], Normalization.LOWER_TRIM, KEY) == 1
    total = reader.hmac_match_count(col, list(expected_counts), Normalization.LOWER_TRIM, KEY)
    assert total == len(tricky_rows)
    # EXACT does not normalize at all
    assert reader.hmac_match_count(col, [vivian], Normalization.EXACT, KEY) == 1
    assert reader.hmac_match_count(col, [], Normalization.EXACT, KEY) == 0
    # digests are compared case-insensitively (hex)
    assert reader.hmac_match_count(col, [vivian.upper()], Normalization.LOWER_TRIM, KEY) == 2


def test_hmac_match_counts_ground_truth_hidden_copies(reader: PgTargetReader, truth: Any) -> None:
    p = primary(truth)
    email = reader.fetch_columns(tref("customer"), ["id"], (p,), ["email", "phone"])
    assert email is not None
    ring = HmacKeyring(KEY)
    col = ColumnRef(tref("marketing_event_log"), "email")
    hidden = {n: c for _t, _c, n, c in truth.subject(p)["hidden_copies"]}
    for norm in (Normalization.EXACT, Normalization.LOWER_TRIM):
        d = ring.digest(email["email"], norm)
        assert reader.hmac_match_count(col, [d], norm, KEY) == hidden[norm.value]
    digits = ring.digest(email["phone"], Normalization.DIGITS_ONLY)
    phone = ColumnRef(tref("customer"), "phone")
    assert reader.hmac_match_count(phone, [digits], Normalization.DIGITS_ONLY, KEY) >= 1


def test_hmac_match_on_jsonb_and_rejections(
    app_conn: Connection, reader: PgTargetReader, truth: Any
) -> None:
    p = primary(truth)
    payload_text = app_conn.execute(
        text("SELECT payload::text FROM app.customer_profile_cache WHERE customer_id = :c"),
        {"c": p},
    ).scalar_one()
    d = hmac.new(KEY, payload_text.encode(), hashlib.sha256).hexdigest()
    col = ColumnRef(tref("customer_profile_cache"), "payload")
    assert reader.hmac_match_count(col, [d], Normalization.EXACT, KEY) == 1
    with pytest.raises(ValidationError):
        reader.hmac_match_count(ColumnRef(tref("customer"), "id"), [d], Normalization.EXACT, KEY)
    with pytest.raises(ConfigurationError):
        reader.hmac_match_count(col, [d], Normalization.EXACT, b"short")


# ------------------------------------------------------------------ derived consistency


def test_symmetric_difference(
    app_conn: Connection,
    reader: PgTargetReader,
    writer: PgTargetWriter,
    catalog: CatalogSnapshot,
    generator: Any,
) -> None:
    ltv_sql = catalog.table(tref("customer_ltv")).definition_sql
    assert ltv_sql is not None
    assert reader.symmetric_difference_count(tref("customer_ltv"), ltv_sql) == 0
    seg = generator.SEGMENT_DEFINITION_SQL
    assert reader.symmetric_difference_count(tref("customer_segment"), seg) == 0
    rev_sql = catalog.table(tref("daily_revenue")).definition_sql
    assert rev_sql is not None
    assert reader.symmetric_difference_count(tref("daily_revenue"), rev_sql) == 0
    # make the matview stale: one customer loses all orders' payments
    app_conn.execute(
        text(
            "DELETE FROM app.payment WHERE order_id IN "
            "(SELECT id FROM app.purchase_order WHERE customer_id IS NOT NULL LIMIT 1)"
        )
    )
    assert reader.symmetric_difference_count(tref("customer_ltv"), ltv_sql) > 0
    writer.refresh_matview(tref("customer_ltv"))
    writer.refresh_matview(tref("daily_revenue"))
    assert reader.symmetric_difference_count(tref("customer_ltv"), ltv_sql) == 0
    assert reader.symmetric_difference_count(tref("daily_revenue"), rev_sql) == 0
    # segment is now stale relative to ltv until recomputed
    n = writer.recompute_table(tref("customer_segment"), seg)
    assert n == app_conn.execute(text("SELECT count(*) FROM app.customer_ltv")).scalar_one()
    assert reader.symmetric_difference_count(tref("customer_segment"), seg) == 0


# ------------------------------------------------------------------ writes


def test_lock_and_delete_with_rowcounts(
    app_conn: Connection, catalog: CatalogSnapshot, truth: Any
) -> None:
    w = PgTargetWriter(app_conn, catalog, batch_size=2)
    p = primary(truth)
    addr = owned(truth, p, "customer_address")
    ticket_keys = owned(truth, p, "support_ticket")
    assert w.lock_rows(tref("customer_address"), ["id"], addr) == len(addr)
    assert w.delete_rows(tref("customer_address"), ["id"], addr + addr) == len(addr)
    assert w.delete_rows(tref("customer_address"), ["id"], addr) == 0
    msgs = app_conn.execute(
        text("SELECT count(*) FROM app.ticket_message WHERE ticket_id = ANY(:t)"),
        {"t": [k[0] for k in ticket_keys]},
    ).scalar_one()
    assert msgs > 0
    assert w.delete_rows(tref("support_ticket"), ["id"], ticket_keys) == len(ticket_keys)
    left = app_conn.execute(
        text("SELECT count(*) FROM app.ticket_message WHERE ticket_id = ANY(:t)"),
        {"t": [k[0] for k in ticket_keys]},
    ).scalar_one()
    assert left == 0  # ON DELETE CASCADE did the rest


def test_fk_violation_is_translated_without_values(writer: PgTargetWriter, truth: Any) -> None:
    p = primary(truth)
    with pytest.raises(TargetQueryError) as info:
        writer.delete_rows(tref("customer"), ["id"], [(p,)])
    err = info.value
    assert err.details["sqlstate"] == "23503"
    assert err.details["table"] == "app.customer"
    assert "constraint" in err.details
    assert "@example.test" not in str(err) and "@example.test" not in repr(err.details)
    assert err.__cause__ is None and err.__suppress_context__


def test_update_columns_null_and_pseudonym(
    app_conn: Connection, writer: PgTargetWriter, truth: Any
) -> None:
    p = primary(truth)
    orders = owned(truth, p, "purchase_order")
    n = writer.update_columns(
        tref("purchase_order"),
        ["id"],
        orders,
        {"shipping_name": PSEUDO, "shipping_address": NULL, "customer_id": NULL},
    )
    assert n == len(orders)
    rows = app_conn.execute(
        text(
            "SELECT id, shipping_name, shipping_address, customer_id FROM app.purchase_order "
            "WHERE id = ANY(:ids) ORDER BY id"
        ),
        {"ids": [o[0] for o in orders]},
    ).all()
    for oid, name, addr, cust in rows:
        assert addr is None and cust is None
        assert re.fullmatch(r"anon_[0-9a-f]{16}", name)
        expected = hmac.new(KEY, str(oid).encode(), hashlib.sha256).hexdigest()[:16]
        assert name == f"anon_{expected}"
    # composite primary key text is joined with ':'
    lines = owned(truth, p, "order_line")[:1]
    with pytest.raises(ValidationError):  # PSEUDONYM on a non-text column
        writer.update_columns(
            tref("order_line"), ["order_id", "line_no"], lines, {"quantity": PSEUDO}
        )


def test_pseudonym_composite_key_and_key_required(
    app_conn: Connection, catalog: CatalogSnapshot, writer: PgTargetWriter
) -> None:
    app_conn.execute(text("ALTER TABLE app.order_line ADD COLUMN note text"))
    tables = PgCatalogReader(app_conn).list_tables(["app"])
    cat = CatalogSnapshot({t.ref: t for t in tables}, ("app",), FP)
    w = PgTargetWriter(app_conn, cat, hmac_key=KEY)
    assert (
        w.update_columns(tref("order_line"), ["order_id", "line_no"], [(1, 1)], {"note": PSEUDO})
        == 1
    )
    note = app_conn.execute(
        text("SELECT note FROM app.order_line WHERE order_id = 1 AND line_no = 1")
    ).scalar_one()
    assert note == "anon_" + hmac.new(KEY, b"1:1", hashlib.sha256).hexdigest()[:16]
    no_key = PgTargetWriter(app_conn, cat)
    with pytest.raises(ConfigurationError):
        no_key.update_columns(
            tref("order_line"), ["order_id", "line_no"], [(1, 1)], {"note": PSEUDO}
        )
    with pytest.raises(ValidationError):
        w.update_columns(tref("order_line"), ["order_id", "line_no"], [(1, 1)], {})
    with pytest.raises(ConfigurationError):
        PgTargetWriter(app_conn, cat, hmac_key=b"short")
    assert "k" * 8 not in repr(w)


def test_dml_only_on_base_tables(writer: PgTargetWriter, generator: Any) -> None:
    with pytest.raises(ValidationError):
        writer.delete_rows(tref("customer_ltv"), ["customer_id"], [(1,)])
    with pytest.raises(ValidationError):
        writer.refresh_matview(tref("customer"))
    with pytest.raises(ValidationError):
        writer.recompute_table(tref("customer_ltv"), generator.SEGMENT_DEFINITION_SQL)


def test_recompute_allow_list(
    app_conn: Connection, catalog: CatalogSnapshot, generator: Any
) -> None:
    w = PgTargetWriter(app_conn, catalog, allowed_sql=[generator.SEGMENT_DEFINITION_SQL])
    with pytest.raises(ValidationError):
        w.recompute_table(tref("customer_segment"), "SELECT 1, 'GOLD'")
    assert w.recompute_table(tref("customer_segment"), generator.SEGMENT_DEFINITION_SQL) > 0


def test_uuid_and_bigint_keys_bind_with_typed_casts(app_conn: Connection) -> None:
    app_conn.execute(
        text(
            "CREATE TABLE app.uuid_keyed (id uuid PRIMARY KEY, n bigint NOT NULL, "
            "amount numeric(12,0) NOT NULL, label varchar(40))"
        )
    )
    u1, u2 = "6f1c2a3e-0000-4000-8000-000000000001", "6f1c2a3e-0000-4000-8000-000000000002"
    app_conn.execute(
        text(
            "INSERT INTO app.uuid_keyed VALUES (CAST(:a AS uuid), 42, 7, 'x'), "
            "(CAST(:b AS uuid), 43, 8, '42')"
        ),
        {"a": u1, "b": u2},
    )
    tables = PgCatalogReader(app_conn).list_tables(["app"])
    cat = CatalogSnapshot({t.ref: t for t in tables}, ("app",), FP)
    r = PgTargetReader(app_conn, cat)
    t = tref("uuid_keyed")
    rows = r.select_by_columns(t, ["id"], [(u2,), (u1,)], ["id", "n", "amount"])
    assert rows == [{"id": u1, "n": 42, "amount": 7}, {"id": u2, "n": 43, "amount": 8}]
    assert r.count_by_columns(t, ["n"], [("42",), (43,)]) == 2  # str '42' vs bigint
    assert r.count_by_columns(t, ["label"], [(42,)]) == 1  # int 42 vs varchar
    assert r.count_by_columns(t, ["id", "n"], [(u1, "42"), (u2, 42)]) == 1
    assert r.count_by_columns(t, ["amount"], [(7,), ("8",)]) == 2
    w = PgTargetWriter(app_conn, cat, hmac_key=KEY)
    assert w.update_columns(t, ["id"], [(u1,)], {"label": PSEUDO}) == 1
    assert w.lock_rows(t, ["n"], [("43",)]) == 1
    assert w.delete_rows(t, ["id"], [(u1,), (u2,)]) == 2
    with pytest.raises(TargetQueryError):
        r.count_by_columns(t, ["id"], [("not-a-uuid",)])


@pytest.mark.parametrize(
    "bad_type",
    ["bigint; DROP TABLE x", 'text" OR 1=1', "int)--", "numeric(4,3) || 1", "Text"],
)
def test_cast_type_rejects_non_catalog_types(bad_type: str) -> None:
    from lae.db._sql import cast_type

    with pytest.raises(ValidationError):
        cast_type(bad_type)
    for ok in (
        "bigint",
        "character varying(20)",
        "timestamp with time zone",
        "numeric(4,3)",
        "vector(384)",
        "text[]",
        "uuid",
    ):
        cast_type(ok)
