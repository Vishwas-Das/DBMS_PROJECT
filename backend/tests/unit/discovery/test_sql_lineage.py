from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from lae.contracts.enums import (
    ArtifactKind,
    EdgeOrigin,
    LineageLevel,
    RefreshStrategy,
    TableKind,
    Traceability,
    Transform,
)
from lae.contracts.types import ArtifactDefinition, KeyMapping
from lae.discovery import (
    SQLGLOT_VERSION,
    build_catalog,
    extract_artifacts_lineage,
    extract_sql_lineage,
)
from lae.discovery import sql_lineage as sl
from lae.errors import UnknownTableError
from lae.lineage import build_lineage_graph

pytestmark = pytest.mark.unit

KP = Traceability.KEY_PRESERVING


def _catalog(kit, extra=()):
    return build_catalog([*kit.demo_tables(), *extra], kit.demo_fks(), ["app"])


def _art(kit, name, sql, kind=ArtifactKind.MATERIALIZED_VIEW, mappings=(), target=None):
    return ArtifactDefinition(
        kit.uid(name),
        name,
        kind,
        target or kit.t(name),
        sql,
        RefreshStrategy.REFRESH_MATVIEW,
        tuple(mappings),
    )


def _table_edges(result):
    return {e.source.name: e for e in result.edges if e.level is LineageLevel.TABLE}


def test_sqlglot_is_the_pinned_version():
    assert SQLGLOT_VERSION == "30.29.0"


def test_demo_artifacts_chain(kit):
    cat = _catalog(kit)
    results = {r.artifact_id: r for r in extract_artifacts_lineage(kit.demo_artifacts(), cat)}
    ltv = results[kit.uid("customer_ltv")]
    assert ltv.traceability is KP
    e = _table_edges(ltv)
    assert (e["customer"].source_columns, e["customer"].target_columns) == (
        ("id",),
        ("customer_id",),
    )
    assert e["payment"].traceability is Traceability.AGGREGATE
    total = [
        c
        for c in ltv.edges
        if c.level is LineageLevel.COLUMN and c.target_columns == ("total_spent",)
    ]
    assert {(c.source.name, c.source_columns[0], c.transform) for c in total} >= {
        ("payment", "amount_cents", Transform.AGGREGATE)
    }

    assert results[kit.uid("daily_revenue")].traceability is Traceability.AGGREGATE

    # customer_ltv is a matview without a PK: its key comes from the upstream KP edge
    seg = results[kit.uid("customer_segment")]
    assert seg.traceability is KP
    se = _table_edges(seg)["customer_ltv"]
    assert (se.source_columns, se.target_columns) == (("customer_id",), ("customer_id",))

    emb = results[kit.uid("document_embedding")]
    assert emb.traceability is KP
    me = _table_edges(emb)["document"]
    assert me.origin is EdgeOrigin.REGISTERED_MAPPING
    assert (me.source_columns, me.target_columns) == (("id",), ("source_document_id",))

    cache = results[kit.uid("customer_profile_cache")]
    assert cache.traceability is KP
    assert _table_edges(cache)["customer"].origin is EdgeOrigin.REGISTERED_MAPPING

    # all of it forms a valid lineage graph, and artifact order follows the chain
    edges = [x for r in results.values() for x in r.edges]
    graph = build_lineage_graph(cat, edges)
    order = graph.artifact_order([kit.t("customer_segment"), kit.t("customer_ltv")])
    assert order == [kit.t("customer_ltv"), kit.t("customer_segment")]


def test_segment_alone_is_aggregate_without_upstream_key(kit):
    # Standalone, customer_ltv has no PK, so its customer_id is not a known key.
    cat = _catalog(kit)
    seg = extract_sql_lineage(_art(kit, "customer_segment", kit.SEGMENT_SQL), cat)
    assert seg.traceability is Traceability.OPAQUE
    keyed = extract_sql_lineage(
        _art(kit, "customer_segment", kit.SEGMENT_SQL),
        cat,
        extra_keys={kit.t("customer_ltv"): [("customer_id",)]},
    )
    assert keyed.traceability is KP


def test_registered_mapping_overrides_inference(kit):
    cat = _catalog(kit)
    sql = "SELECT email AS customer_id, '{}'::jsonb AS payload FROM app.customer"
    inferred = extract_sql_lineage(_art(kit, "customer_profile_cache", sql), cat)
    assert inferred.traceability is Traceability.OPAQUE
    mapped = extract_sql_lineage(
        _art(
            kit,
            "customer_profile_cache",
            sql,
            mappings=[KeyMapping(kit.t("customer"), ("id",), ("customer_id",))],
        ),
        cat,
    )
    assert mapped.traceability is KP
    edges = _table_edges(mapped)
    assert set(edges) == {"customer"}
    assert edges["customer"].origin is EdgeOrigin.REGISTERED_MAPPING
    assert edges["customer"].details == {"evidence": "registered_mapping"}
    assert all(e.origin is EdgeOrigin.REGISTERED_MAPPING for e in mapped.edges)


def test_mapping_and_unmapped_inferred_sources_coexist(kit):
    cat = _catalog(kit)
    sql = (
        "SELECT d.id AS source_document_id, NULL::text AS embedding, d.id AS id "
        "FROM app.document d JOIN app.customer c ON c.id = d.customer_id"
    )
    art = _art(
        kit,
        "document_embedding",
        sql,
        ArtifactKind.EMBEDDING_TABLE,
        [KeyMapping(kit.t("document"), ("id",), ("source_document_id",))],
    )
    r = extract_sql_lineage(art, cat)
    e = _table_edges(r)
    assert e["document"].origin is EdgeOrigin.REGISTERED_MAPPING
    assert e["customer"].origin is EdgeOrigin.SQL_LINEAGE
    assert e["customer"].traceability is Traceability.AGGREGATE  # control-only source
    assert r.traceability is KP


@pytest.mark.parametrize(
    "mapping",
    ["unknown_source", "unknown_source_column", "unknown_target_column", "mismatched", "empty"],
)
def test_invalid_mapping_makes_artifact_opaque(kit, mapping):
    cat = _catalog(kit)
    m = {
        "unknown_source": KeyMapping(kit.t("ghost"), ("id",), ("customer_id",)),
        "unknown_source_column": KeyMapping(kit.t("customer"), ("nope",), ("customer_id",)),
        "unknown_target_column": KeyMapping(kit.t("customer"), ("id",), ("nope",)),
        "mismatched": KeyMapping(kit.t("customer"), ("id", "email"), ("customer_id",)),
        "empty": KeyMapping(kit.t("customer"), (), ()),
    }[mapping]
    r = extract_sql_lineage(_art(kit, "customer_profile_cache", None, ArtifactKind.CACHE, [m]), cat)
    assert r.traceability is Traceability.OPAQUE
    assert any("key mapping" in w for w in r.warnings)
    assert r.edges == ()


def test_cache_without_sql_or_mapping_is_opaque(kit):
    r = extract_sql_lineage(
        _art(kit, "customer_profile_cache", None, ArtifactKind.CACHE), _catalog(kit)
    )
    assert r.traceability is Traceability.OPAQUE
    assert r.warnings == ("no definition SQL and no key mapping",)
    blank = extract_sql_lineage(
        _art(kit, "customer_profile_cache", "   ", ArtifactKind.CACHE), _catalog(kit)
    )
    assert blank.traceability is Traceability.OPAQUE


def test_external_artifact_is_opaque_without_edges(kit):
    art = _art(
        kit,
        "crm_export",
        "SELECT * FROM app.customer",
        ArtifactKind.EXTERNAL,
        target=kit.t("crm_export", "external"),
    )
    r = extract_sql_lineage(art, _catalog(kit))
    assert r.traceability is Traceability.OPAQUE
    assert r.edges == ()
    assert r.warnings == ("external artifact is not inspectable",)


def test_unknown_target_raises(kit):
    with pytest.raises(UnknownTableError):
        extract_sql_lineage(_art(kit, "ghost", "SELECT id FROM app.customer"), _catalog(kit))


def test_self_reference_and_missing_output_columns_warn(kit):
    cat = _catalog(kit)
    r = extract_sql_lineage(
        _art(
            kit,
            "customer_segment",
            "SELECT s.customer_id, s.segment, c.email AS extra "
            "FROM app.customer_segment s JOIN app.customer c ON c.id = s.customer_id",
        ),
        cat,
    )
    assert any("own target" in w for w in r.warnings)
    assert any("output column extra" in w for w in r.warnings)
    assert "customer_segment" not in _table_edges(r)
    assert all(e.target_columns != ("extra",) for e in r.edges)


def test_case_sensitive_identifiers(kit):
    tables = [
        kit.table("Customer", [("Id", "bigint"), ("Email", "text")], ("Id",)),
        kit.table("art", [("Id", "bigint"), ("mail", "text")]),
    ]
    cat = build_catalog(tables, [], ["app"])
    r = extract_sql_lineage(
        _art(kit, "art", 'SELECT "Id", "Email" AS mail FROM app."Customer"'), cat
    )
    assert r.traceability is KP
    e = _table_edges(r)["Customer"]
    assert (e.source_columns, e.target_columns) == (("Id",), ("Id",))


def test_ambiguous_unqualified_table_uses_schema_order(kit):
    tables = [
        kit.table("thing", [("id", "int")], ("id",), schema="app"),
        kit.table("thing", [("id", "int")], ("id",), schema="zeta"),
        kit.table("art", [("id", "int")], schema="app"),
    ]
    cat = build_catalog(tables, [], ["zeta", "app"])
    r = extract_sql_lineage(_art(kit, "art", "SELECT id FROM thing"), cat)
    assert [e.source for e in r.edges if e.level is LineageLevel.TABLE] == [kit.t("thing")]


def test_internal_errors_never_escape(kit, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("sqlglot bug")

    monkeypatch.setattr(sl, "_analyse", boom)
    r = extract_sql_lineage(_art(kit, "customer_ltv", kit.LTV_SQL), _catalog(kit))
    assert r.traceability is Traceability.OPAQUE
    assert any("internal analysis error (RuntimeError)" in w for w in r.warnings)
    assert {e.source.name for e in r.edges} == {"customer", "purchase_order", "payment"}


def test_path_explosion_is_opaque(kit, monkeypatch):
    monkeypatch.setattr(sl, "_MAX_PATHS", 0)
    r = extract_sql_lineage(_art(kit, "customer_ltv", kit.LTV_SQL), _catalog(kit))
    assert r.traceability is Traceability.OPAQUE
    assert "lineage too complex to analyse" in r.warnings


def test_artifacts_lineage_reaches_fixpoint_over_long_chains(kit):
    tables = [
        kit.table("base", [("id", "int"), ("v", "int")], ("id",)),
        kit.table("m1", [("id", "int"), ("v", "int")], kind=TableKind.MATERIALIZED_VIEW),
        kit.table("m2", [("id", "int"), ("v", "int")]),
        kit.table("m3", [("id", "int"), ("v", "int")]),
    ]
    cat = build_catalog(tables, [], ["app"])
    arts = [
        _art(kit, "m3", "SELECT id, v FROM app.m2"),
        _art(kit, "m2", "SELECT id, v + 1 AS v FROM app.m1"),
        _art(kit, "m1", "SELECT id, v FROM app.base"),
    ]
    results = extract_artifacts_lineage(arts, cat)
    assert [r.artifact_id for r in results] == [kit.uid("m1"), kit.uid("m2"), kit.uid("m3")]
    assert all(r.traceability is KP for r in results)


_SQL_WORDS = st.sampled_from(
    [
        "SELECT",
        "FROM",
        "WHERE",
        "GROUP BY",
        "JOIN",
        "ON",
        "UNION",
        "WITH",
        "AS",
        "(",
        ")",
        ",",
        "*",
        "app.customer",
        "app.purchase_order",
        "id",
        "email",
        "customer_id",
        "count(*)",
        "sum(total_cents)",
        "=",
        "'x'",
        "1",
        ";",
        "LATERAL",
        "RECURSIVE",
        "over ()",
        "::text",
        "$$",
        '"',
        "CASE WHEN",
        "END",
        "NULL",
    ]
)


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(st.one_of(st.text(max_size=80), st.lists(_SQL_WORDS, max_size=14).map(" ".join)))
def test_never_raises_on_arbitrary_sql(kit, sql):
    cat = _catalog(kit)
    r = extract_sql_lineage(_art(kit, "customer_ltv", sql), cat)
    assert r.traceability in set(Traceability)
    if r.traceability is Traceability.OPAQUE:
        assert r.warnings
    build_lineage_graph(cat, r.edges)  # whatever comes out is structurally valid
