"""read_catalog / fingerprint / schema_drift through PgCatalogReader on the seeded `app` schema."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Connection, text

from lae.contracts.enums import TableKind
from lae.contracts.types import TableRef
from lae.db.catalog_queries import PgCatalogReader
from lae.discovery import read_catalog, schema_drift

pytestmark = pytest.mark.integration

APP = "app"
KIND = {
    "r": TableKind.TABLE,
    "p": TableKind.TABLE,
    "v": TableKind.VIEW,
    "m": TableKind.MATERIALIZED_VIEW,
}


def t(name: str) -> TableRef:
    return TableRef(APP, name)


def _independent_relations(conn: Connection) -> dict[TableRef, TableKind]:
    rows = conn.execute(
        text(
            "SELECT c.relname, c.relkind FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'app' AND c.relkind IN ('r','p','v','m') AND NOT c.relispartition"
        )
    ).all()
    return {t(name): KIND[kind] for name, kind in rows}


@pytest.fixture
def reader(seeded_db: tuple[Any, Any], app_conn: Connection) -> PgCatalogReader:
    return PgCatalogReader(app_conn)


def test_all_relations_and_kinds(reader: PgCatalogReader, app_conn: Connection) -> None:
    catalog = read_catalog(reader, [APP])
    expected = _independent_relations(app_conn)
    assert {ref: info.kind for ref, info in catalog.tables.items()} == expected
    assert len(expected) == 17
    assert catalog.schemas == (APP,)
    assert list(catalog.tables) == sorted(catalog.tables)
    # lae's own schema is never part of an `app` snapshot
    assert all(ref.schema == APP for ref in catalog.tables)


def test_columns_match_pg_attribute(reader: PgCatalogReader, app_conn: Connection) -> None:
    catalog = read_catalog(reader, [APP])
    rows = app_conn.execute(
        text(
            "SELECT c.relname, a.attname, a.attnum, format_type(a.atttypid, a.atttypmod), "
            "NOT a.attnotnull FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'app' "
            "AND c.relkind IN ('r','p','v','m') AND a.attnum > 0 AND NOT a.attisdropped"
        )
    ).all()
    expected: dict[TableRef, set[tuple[str, int, str, bool]]] = {}
    for rel, name, num, dtype, nullable in rows:
        expected.setdefault(t(rel), set()).add((name, int(num), dtype, bool(nullable)))
    got = {
        ref: {(c.name, c.ordinal, c.data_type, c.is_nullable) for c in info.columns}
        for ref, info in catalog.tables.items()
    }
    assert got == expected
    assert catalog.table(t("document_embedding")).column("embedding").data_type == "vector(384)"


def test_primary_keys_composite_and_matviews_without_pk(reader: PgCatalogReader) -> None:
    catalog = read_catalog(reader, [APP])
    pk = {ref.name: info.primary_key for ref, info in catalog.tables.items()}
    assert pk["order_line"] == ("order_id", "line_no")  # composite, in key order
    assert pk["loyalty_account"] == ("customer_id",)  # PK = FK
    assert pk["customer_segment"] == ("customer_id",)  # CTAS + ALTER TABLE ADD PRIMARY KEY
    assert pk["marketing_event_log"] == ()  # hidden copy: no PK
    # PostgreSQL cannot declare a PK on a matview; the unique index is NOT reported (Lead, CR-1 §3)
    for mv in ("customer_ltv", "daily_revenue"):
        info = catalog.table(t(mv))
        assert info.kind is TableKind.MATERIALIZED_VIEW
        assert info.primary_key == ()
        assert info.definition_sql is not None and info.definition_sql.lstrip().startswith("SELECT")
    assert catalog.table(t("customer")).definition_sql is None


def test_composite_fk_order_from_reader(reader: PgCatalogReader) -> None:
    fks = {f.constraint_name: f for f in reader.list_foreign_keys([APP])}
    shipment = fks["shipment_order_line_fkey"]
    assert shipment.child == t("shipment") and shipment.parent == t("order_line")
    assert shipment.child_columns == ("order_id", "line_no")
    assert shipment.parent_columns == ("order_id", "line_no")


def test_fingerprint_stable_across_reads_and_analyze(
    reader: PgCatalogReader, app_conn: Connection
) -> None:
    first = read_catalog(reader, [APP])
    second = read_catalog(reader, [APP])
    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 64 and first.fingerprint == first.fingerprint.lower()
    assert not schema_drift(first, second).has_drift
    nested = app_conn.begin_nested()
    try:
        app_conn.execute(
            text(
                "INSERT INTO app.product (sku, name, price_cents) VALUES "
                "('SKU-DRIFT', 'drift probe', 1)"
            )
        )
        app_conn.execute(text("ANALYZE app.product"))
        after = read_catalog(reader, [APP])
    finally:
        nested.rollback()
    # data changes and statistics (reltuples) are not schema drift
    assert after.fingerprint == first.fingerprint
    assert not schema_drift(first, after).has_drift


def test_fingerprint_changes_on_alter_and_drift_reports_it(
    reader: PgCatalogReader, app_conn: Connection
) -> None:
    before = read_catalog(reader, [APP])
    nested = app_conn.begin_nested()
    try:
        app_conn.execute(text("ALTER TABLE app.customer ADD COLUMN tmp int"))
        during = read_catalog(reader, [APP])
    finally:
        nested.rollback()
    after = read_catalog(reader, [APP])

    assert during.fingerprint != before.fingerprint
    assert "tmp" in {c.name for c in during.table(t("customer")).columns}
    drift = schema_drift(before, during)
    assert drift.has_drift and drift.fingerprint_changed
    assert drift.changed_tables == (t("customer"),)
    assert drift.added_tables == () and drift.removed_tables == ()
    # rolled back: the schema (and its fingerprint) is exactly as before
    assert after.fingerprint == before.fingerprint
    assert not schema_drift(before, after).has_drift


def test_fk_only_change_is_fingerprint_drift(reader: PgCatalogReader, app_conn: Connection) -> None:
    """FK changes are invisible in CatalogSnapshot.tables; the fingerprint must still move."""
    before = read_catalog(reader, [APP])
    nested = app_conn.begin_nested()
    try:
        app_conn.execute(
            text(
                "ALTER TABLE app.document_embedding ADD CONSTRAINT tmp_embedding_document_fkey "
                "FOREIGN KEY (source_document_id) REFERENCES app.document (id) NOT VALID"
            )
        )
        during = read_catalog(reader, [APP])
    finally:
        nested.rollback()
    drift = schema_drift(before, during)
    assert drift.fingerprint_changed and drift.has_drift
    assert drift.changed_tables == ()


def test_new_table_is_added_drift(reader: PgCatalogReader, app_conn: Connection) -> None:
    before = read_catalog(reader, [APP])
    nested = app_conn.begin_nested()
    try:
        app_conn.execute(text("CREATE TABLE app.tmp_drift_probe (id int PRIMARY KEY)"))
        during = read_catalog(reader, [APP])
    finally:
        nested.rollback()
    drift = schema_drift(before, during)
    assert drift.added_tables == (t("tmp_drift_probe"),)
    assert drift.removed_tables == () and drift.fingerprint_changed
    assert schema_drift(during, before).removed_tables == (t("tmp_drift_probe"),)
