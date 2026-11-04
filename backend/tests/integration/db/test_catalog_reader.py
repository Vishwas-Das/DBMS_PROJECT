"""PgCatalogReader against the seeded demo schema."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Connection, Engine

from lae.contracts.enums import OnDeleteAction, TableKind
from lae.contracts.ports import CatalogReader
from lae.contracts.types import ColumnRef, TableRef
from lae.db.catalog_queries import PgCatalogReader
from lae.errors import CatalogError, UnknownColumnError, UnknownTableError, ValidationError

pytestmark = pytest.mark.integration


def tref(name: str) -> TableRef:
    return TableRef("app", name)


@pytest.fixture
def reader(seeded_db: tuple[Engine, Any], app_conn: Connection) -> PgCatalogReader:
    return PgCatalogReader(app_conn)


def test_implements_port(reader: PgCatalogReader) -> None:
    assert isinstance(reader, CatalogReader)


def test_lists_every_relation_with_kind_and_pk(reader: PgCatalogReader) -> None:
    tables = {t.ref.name: t for t in reader.list_tables(["app"])}
    assert len(tables) == 17
    kinds = {n: t.kind for n, t in tables.items()}
    assert kinds["customer_ltv"] is TableKind.MATERIALIZED_VIEW
    assert kinds["daily_revenue"] is TableKind.MATERIALIZED_VIEW
    assert kinds["customer_segment"] is TableKind.TABLE
    assert sum(k is TableKind.TABLE for k in kinds.values()) == 15
    assert tables["order_line"].primary_key == ("order_id", "line_no")
    assert tables["loyalty_account"].primary_key == ("customer_id",)
    assert tables["customer_segment"].primary_key == ("customer_id",)
    assert tables["marketing_event_log"].primary_key == ()
    assert tables["customer_ltv"].primary_key == ()  # unique index, not a PK
    assert (
        tables["customer_ltv"].definition_sql
        and "GROUP BY" in tables["customer_ltv"].definition_sql
    )
    assert tables["customer"].definition_sql is None
    emb = {c.name: c for c in tables["document_embedding"].columns}
    assert emb["embedding"].data_type == "vector(384)"
    cust = tables["customer"].columns
    assert [c.name for c in cust] == [
        "id",
        "full_name",
        "email",
        "phone",
        "referrer_id",
        "created_at",
    ]
    assert [c.ordinal for c in cust] == [1, 2, 3, 4, 5, 6]
    assert {c.name: c.is_nullable for c in cust}["phone"] is True
    assert {c.name: c.is_nullable for c in cust}["email"] is False
    assert tables["customer"].estimated_rows == 20  # ANALYZEd by the generator
    # deterministic order
    names = [t.ref for t in reader.list_tables(["app"])]
    assert names == sorted(names)


def test_engine_schema_is_not_listed_unless_asked(reader: PgCatalogReader) -> None:
    assert all(t.ref.schema == "app" for t in reader.list_tables(["app"]))
    assert reader.list_tables(["no_such_schema"]) == []
    with pytest.raises(CatalogError):
        reader.list_tables([])


def test_foreign_keys_composite_order_and_actions(reader: PgCatalogReader) -> None:
    fks = reader.list_foreign_keys(["app"])
    assert len(fks) == 12
    by_child = {(f.child.name, f.child_columns): f for f in fks}
    ship = by_child[("shipment", ("order_id", "line_no"))]
    assert ship.parent == tref("order_line")
    assert ship.parent_columns == ("order_id", "line_no")
    assert ship.constraint_name == "shipment_order_line_fkey"
    expected = {
        ("customer", ("referrer_id",)): OnDeleteAction.SET_NULL,
        ("customer_address", ("customer_id",)): OnDeleteAction.CASCADE,
        ("loyalty_account", ("customer_id",)): OnDeleteAction.NO_ACTION,
        ("purchase_order", ("customer_id",)): OnDeleteAction.RESTRICT,
        ("order_line", ("order_id",)): OnDeleteAction.CASCADE,
        ("order_line", ("product_id",)): OnDeleteAction.NO_ACTION,
        ("shipment", ("order_id", "line_no")): OnDeleteAction.NO_ACTION,
        ("payment", ("order_id",)): OnDeleteAction.NO_ACTION,
        ("support_ticket", ("customer_id",)): OnDeleteAction.SET_NULL,
        ("ticket_message", ("ticket_id",)): OnDeleteAction.CASCADE,
        ("ticket_message", ("author_customer_id",)): OnDeleteAction.NO_ACTION,
        ("document", ("customer_id",)): OnDeleteAction.CASCADE,
    }
    assert {k: f.on_delete for k, f in by_child.items()} == expected
    assert by_child[("customer", ("referrer_id",))].parent == tref("customer")
    assert all(f.on_update is OnDeleteAction.NO_ACTION and not f.deferrable for f in fks)
    assert [(f.child, f.constraint_name) for f in fks] == sorted(
        (f.child, f.constraint_name) for f in fks
    )


def test_sample_values_deterministic(reader: PgCatalogReader) -> None:
    col = ColumnRef(tref("marketing_event_log"), "email")
    a = reader.sample_values(col, 10)
    assert a == reader.sample_values(col, 10)
    assert len(a) == len(set(a)) == 10
    ids = reader.sample_values(ColumnRef(tref("customer"), "id"), 1000)
    assert sorted(ids) == list(range(1, 21)) and all(isinstance(i, int) for i in ids)
    with pytest.raises(ValidationError):
        reader.sample_values(col, 0)


def test_count_contained(reader: PgCatalogReader) -> None:
    child = ColumnRef(tref("marketing_event_log"), "email")
    parent = ColumnRef(tref("customer"), "email")
    sample = reader.sample_values(child, 1000)
    contained = reader.count_contained(child, parent, sample)
    # every exact copy is contained; only the two case/whitespace variants are not
    assert contained == len(sample) - 2
    assert reader.count_contained(child, parent, []) == 0
    ids = ColumnRef(tref("purchase_order"), "customer_id")
    assert reader.count_contained(ids, ColumnRef(tref("customer"), "id"), [1, 2, 999]) == 2


def test_unknown_objects_rejected(reader: PgCatalogReader) -> None:
    with pytest.raises(UnknownTableError):
        reader.sample_values(ColumnRef(tref("nope"), "id"), 5)
    with pytest.raises(UnknownColumnError):
        reader.sample_values(ColumnRef(tref("customer"), "nope"), 5)
    with pytest.raises(UnknownTableError):
        reader.sample_values(ColumnRef(tref('customer"; DROP TABLE app.customer; --'), "id"), 5)
    with pytest.raises(UnknownColumnError):
        reader.count_contained(
            ColumnRef(tref("customer"), "id) OR (1=1"), ColumnRef(tref("customer"), "id"), [1]
        )
