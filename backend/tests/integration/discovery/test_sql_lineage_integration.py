"""SQL lineage on the REAL pg_get_viewdef text of the demo matviews + the registered CTAS SQL.

Observed viewdef text (PostgreSQL 16, ``pg_get_viewdef(oid, true)``), recorded verbatim:

customer_ltv::

     SELECT o.customer_id,
        count(DISTINCT o.id) AS order_count,
        sum(p.amount_cents)::bigint AS total_spent
       FROM app.purchase_order o
         JOIN app.payment p ON p.order_id = o.id
      WHERE o.customer_id IS NOT NULL
      GROUP BY o.customer_id;

daily_revenue (single-table: PostgreSQL drops the ``p.`` qualifier, adds ``::text`` to the
literal and wraps the GROUP BY expression in an extra pair of parentheses)::

     SELECT (paid_at AT TIME ZONE 'UTC'::text)::date AS day,
        sum(amount_cents)::bigint AS revenue_cents,
        count(*) AS payment_count
       FROM app.payment p
      GROUP BY ((paid_at AT TIME ZONE 'UTC'::text)::date);
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from lae.contracts.enums import (
    EdgeOrigin,
    EdgeStatus,
    LineageLevel,
    Traceability,
    Transform,
)
from lae.contracts.types import TableRef
from lae.discovery import extract_artifacts_lineage, extract_sql_lineage

pytestmark = pytest.mark.integration

APP = "app"


def t(name: str) -> TableRef:
    return TableRef(APP, name)


def _table_edges(result: Any) -> dict[TableRef, Any]:
    return {e.source: e for e in result.edges if e.level is LineageLevel.TABLE}


def _column_pairs(result: Any) -> set[tuple[str, str, str, Transform]]:
    return {
        (e.source.name, e.source_columns[0], e.target_columns[0], e.transform)
        for e in result.edges
        if e.level is LineageLevel.COLUMN
    }


def test_viewdef_text_is_the_registered_definition(pipeline: Any) -> None:
    for mv in ("customer_ltv", "daily_revenue"):
        viewdef = pipeline.conn.execute(
            text("SELECT pg_get_viewdef(to_regclass(:r), true)"), {"r": f"app.{mv}"}
        ).scalar_one()
        assert pipeline.catalog.table(t(mv)).definition_sql == viewdef
        artifact = next(a for a in pipeline.artifacts if a.target == t(mv))
        assert artifact.definition_sql == viewdef  # registered by the seeder from the viewdef
        assert artifact.name == mv  # the registered entry replaced the auto-registered one
    ltv = pipeline.catalog.table(t("customer_ltv")).definition_sql
    daily = pipeline.catalog.table(t("daily_revenue")).definition_sql
    assert "JOIN app.payment p ON p.order_id = o.id" in ltv
    assert "GROUP BY ((paid_at AT TIME ZONE 'UTC'::text)::date)" in daily


def test_extra_keys_are_fk_child_columns_of_the_subject_chain(pipeline: Any) -> None:
    keys = pipeline.extra_keys
    assert ("customer_id",) in keys[t("purchase_order")]
    assert ("order_id", "line_no") in keys[t("shipment")]  # composite, in order
    assert ("author_customer_id",) in keys[t("ticket_message")]
    # product is upstream reference data: never a subject-chain key source
    assert t("product") not in keys
    assert ("product_id",) not in keys.get(t("order_line"), set())


def test_customer_ltv_key_preserving_via_customer_id(pipeline: Any) -> None:
    r = pipeline.lineage_for(t("customer_ltv"))
    assert r.traceability is Traceability.KEY_PRESERVING
    assert r.warnings == ()
    edges = _table_edges(r)
    assert set(edges) == {t("purchase_order"), t("payment")}
    po = edges[t("purchase_order")]
    assert po.traceability is Traceability.KEY_PRESERVING
    assert po.origin is EdgeOrigin.SQL_LINEAGE and po.status is EdgeStatus.CONFIRMED
    assert (po.source_columns, po.target_columns) == (("customer_id",), ("customer_id",))
    assert po.transform is Transform.IDENTITY
    assert po.details["key_mapping"] == "inferred"
    assert po.details["sqlglot_version"]
    pay = edges[t("payment")]
    assert pay.traceability is Traceability.AGGREGATE and pay.source_columns == ()
    pairs = _column_pairs(r)
    assert ("purchase_order", "customer_id", "customer_id", Transform.IDENTITY) in pairs
    assert ("purchase_order", "id", "order_count", Transform.AGGREGATE) in pairs
    assert ("payment", "amount_cents", "total_spent", Transform.AGGREGATE) in pairs
    # control lineage: p.order_id only appears in JOIN ... ON
    assert ("payment", "order_id", "customer_id", Transform.JOIN_KEY) in pairs
    assert all(tr is not Transform.FILTER for (_s, _c, _o, tr) in pairs)


def test_customer_ltv_without_extra_keys_is_aggregate(pipeline: Any) -> None:
    """Default (PK-only) candidate keys: GROUP BY customer_id is only AGGREGATE (CR-1 §2)."""
    artifact = next(a for a in pipeline.artifacts if a.target == t("customer_ltv"))
    r = extract_sql_lineage(artifact, pipeline.catalog)
    assert r.traceability is Traceability.AGGREGATE
    # ... and the downstream CTAS loses its key too, which makes it OPAQUE
    chain = extract_artifacts_lineage(pipeline.artifacts, pipeline.catalog)
    by_target = {a.artifact_id: a.target for a in pipeline.artifacts}
    trace = {by_target[x.artifact_id]: x.traceability for x in chain}
    assert trace[t("customer_ltv")] is Traceability.AGGREGATE
    assert trace[t("customer_segment")] is Traceability.OPAQUE


def test_daily_revenue_aggregate(pipeline: Any) -> None:
    r = pipeline.lineage_for(t("daily_revenue"))
    assert r.traceability is Traceability.AGGREGATE
    assert r.warnings == ()
    edges = _table_edges(r)
    assert set(edges) == {t("payment")}
    assert edges[t("payment")].traceability is Traceability.AGGREGATE
    assert edges[t("payment")].transform is Transform.AGGREGATE
    pairs = _column_pairs(r)
    assert ("payment", "amount_cents", "revenue_cents", Transform.AGGREGATE) in pairs
    assert ("payment", "paid_at", "day", Transform.EXPRESSION) in pairs  # AT TIME ZONE + cast


def test_customer_segment_key_preserving_via_upstream_matview_key(pipeline: Any) -> None:
    r = pipeline.lineage_for(t("customer_segment"))
    artifact = next(a for a in pipeline.artifacts if a.target == t("customer_segment"))
    assert artifact.definition_sql.startswith("SELECT l.customer_id, CASE")
    assert r.traceability is Traceability.KEY_PRESERVING
    edges = _table_edges(r)
    assert set(edges) == {t("customer_ltv")}
    e = edges[t("customer_ltv")]
    assert (e.source_columns, e.target_columns) == (("customer_id",), ("customer_id",))
    pairs = _column_pairs(r)
    assert ("customer_ltv", "customer_id", "customer_id", Transform.IDENTITY) in pairs
    assert ("customer_ltv", "total_spent", "segment", Transform.EXPRESSION) in pairs


def test_registered_mappings_and_determinism(pipeline: Any) -> None:
    for target, source, scol, tcol in (
        ("document_embedding", "document", "id", "source_document_id"),
        ("customer_profile_cache", "customer", "id", "customer_id"),
    ):
        r = pipeline.lineage_for(t(target))
        assert r.traceability is Traceability.KEY_PRESERVING
        (edge,) = (e for e in r.edges if e.level is LineageLevel.TABLE)
        assert edge.origin is EdgeOrigin.REGISTERED_MAPPING and edge.source == t(source)
        assert (edge.source_columns, edge.target_columns) == ((scol,), (tcol,))
    again = extract_artifacts_lineage(
        pipeline.artifacts, pipeline.catalog, extra_keys=pipeline.extra_keys
    )
    assert again == pipeline.lineage


def test_external_artifact_is_not_inspected(pipeline: Any) -> None:
    targets = {a.target for a in pipeline.artifacts}
    assert TableRef("external", "crm_export_files") not in targets
