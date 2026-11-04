"""Declared-FK edges vs an independent reading of pg_constraint (pg_get_constraintdef text)."""

from __future__ import annotations

import re
from typing import Any

import pytest
from sqlalchemy import Connection, text

from lae.contracts.enums import (
    EdgeOrigin,
    EdgeStatus,
    LineageLevel,
    OnDeleteAction,
    Traceability,
    Transform,
)
from lae.contracts.types import TableRef
from lae.db.catalog_queries import PgCatalogReader
from lae.discovery import discover_foreign_keys, read_catalog

pytestmark = pytest.mark.integration

APP = "app"
_DEF = re.compile(
    r"^FOREIGN KEY \((?P<child>[^)]*)\) REFERENCES (?P<parent>[\w.]+)\((?P<pcols>[^)]*)\)"
    r"(?P<rest>.*)$"
)
_ON_DELETE = {
    "CASCADE": OnDeleteAction.CASCADE,
    "SET NULL": OnDeleteAction.SET_NULL,
    "SET DEFAULT": OnDeleteAction.SET_DEFAULT,
    "RESTRICT": OnDeleteAction.RESTRICT,
}
_EXPECTED_ON_DELETE = {  # from db/fixtures/app_schema.sql
    "customer_referrer_id_fkey": OnDeleteAction.SET_NULL,
    "customer_address_customer_id_fkey": OnDeleteAction.CASCADE,
    "loyalty_account_customer_id_fkey": OnDeleteAction.NO_ACTION,
    "purchase_order_customer_id_fkey": OnDeleteAction.RESTRICT,
    "order_line_order_id_fkey": OnDeleteAction.CASCADE,
    "order_line_product_id_fkey": OnDeleteAction.NO_ACTION,
    "shipment_order_line_fkey": OnDeleteAction.NO_ACTION,
    "payment_order_id_fkey": OnDeleteAction.NO_ACTION,
    "support_ticket_customer_id_fkey": OnDeleteAction.SET_NULL,
    "ticket_message_ticket_id_fkey": OnDeleteAction.CASCADE,
    "ticket_message_author_customer_id_fkey": OnDeleteAction.NO_ACTION,
    "document_customer_id_fkey": OnDeleteAction.CASCADE,
}

FkTuple = tuple[str, TableRef, TableRef, tuple[str, ...], tuple[str, ...], OnDeleteAction]


def _split(cols: str) -> tuple[str, ...]:
    return tuple(c.strip().strip('"') for c in cols.split(","))


def _independent_fks(conn: Connection) -> set[FkTuple]:
    """Parse `pg_get_constraintdef` (a different code path than the reader's attnum join)."""
    rows = conn.execute(
        text(
            "SELECT c.conname, cl.relname, pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "JOIN pg_class cl ON cl.oid = c.conrelid JOIN pg_namespace n ON n.oid = "
            "cl.relnamespace WHERE c.contype = 'f' AND n.nspname = 'app'"
        )
    ).all()
    out: set[FkTuple] = set()
    for conname, child, definition in rows:
        m = _DEF.match(definition)
        assert m is not None, definition
        parent = m["parent"] if "." in m["parent"] else f"{APP}.{m['parent']}"
        on_delete = OnDeleteAction.NO_ACTION
        rest = m["rest"]
        for phrase, action in _ON_DELETE.items():
            if f"ON DELETE {phrase}" in rest:
                on_delete = action
        out.add(
            (
                conname,
                TableRef(APP, child),
                TableRef.parse(parent),
                _split(m["child"]),
                _split(m["pcols"]),
                on_delete,
            )
        )
    return out


@pytest.fixture
def edges(seeded_db: tuple[Any, Any], app_conn: Connection) -> list[Any]:
    reader = PgCatalogReader(app_conn)
    return discover_foreign_keys(reader, read_catalog(reader, [APP]))


def test_every_declared_fk_has_a_table_edge(edges: list[Any], app_conn: Connection) -> None:
    expected = _independent_fks(app_conn)
    assert len(expected) == 12
    table_edges = [e for e in edges if e.level is LineageLevel.TABLE]
    got = {
        (
            e.constraint_name,
            e.target,
            e.source,
            e.target_columns,
            e.source_columns,
            e.on_delete,
        )
        for e in table_edges
    }
    assert got == expected
    assert len(table_edges) == len(expected)  # one TABLE edge per constraint, no duplicates
    assert {e.constraint_name: e.on_delete for e in table_edges} == _EXPECTED_ON_DELETE


def test_edge_attributes_and_evidence(edges: list[Any]) -> None:
    for e in edges:
        assert e.origin is EdgeOrigin.DECLARED_FK
        assert e.status is EdgeStatus.CONFIRMED
        assert e.transform is Transform.FOREIGN_KEY
        assert e.traceability is Traceability.KEY_PRESERVING
        assert e.confidence == 1.0
        assert e.artifact_id is None
        assert e.details["evidence"] == "pg_constraint"
        assert e.details["constraint"] == e.constraint_name
        assert e.details["on_delete"] == e.on_delete.value
        assert e.details["on_update"] == OnDeleteAction.NO_ACTION.value
        assert e.details["deferrable"] is False


def test_column_edges_one_per_pair_in_order(edges: list[Any]) -> None:
    table_edges = {e.constraint_name: e for e in edges if e.level is LineageLevel.TABLE}
    column_edges = [e for e in edges if e.level is LineageLevel.COLUMN]
    assert len(column_edges) == sum(len(e.source_columns) for e in table_edges.values())
    shipment = sorted(
        (e for e in column_edges if e.constraint_name == "shipment_order_line_fkey"),
        key=lambda e: e.details["pair_index"],
    )
    assert [(e.source_columns, e.target_columns) for e in shipment] == [
        (("order_id",), ("order_id",)),
        (("line_no",), ("line_no",)),
    ]
    assert all(e.details["pair_count"] == 2 for e in shipment)
    composite = table_edges["shipment_order_line_fkey"]
    assert composite.source_columns == ("order_id", "line_no")  # never split into two joins
    assert composite.target_columns == ("order_id", "line_no")


def test_edge_ids_deterministic(edges: list[Any], app_conn: Connection) -> None:
    reader = PgCatalogReader(app_conn)
    again = discover_foreign_keys(reader, read_catalog(reader, [APP]))
    assert [e.edge_id for e in again] == [e.edge_id for e in edges]
    assert len({e.edge_id for e in edges}) == len(edges)


def test_tables_without_declared_fk_have_no_fk_edges(edges: list[Any]) -> None:
    targets = {e.target.name for e in edges}
    for name in ("marketing_event_log", "document_embedding", "customer_profile_cache"):
        assert name not in targets  # only inference / registered mappings / residual scan
