from __future__ import annotations

import pytest

from lae.contracts.enums import (
    ArtifactKind,
    EdgeOrigin,
    EdgeStatus,
    ErasureStrategy,
    LineageLevel,
    PiiCategory,
    RefreshStrategy,
    TableKind,
)
from lae.contracts.types import ArtifactDefinition, ColumnRef
from lae.discovery import (
    artifacts_from_catalog,
    auto_artifact_id,
    build_catalog,
    external_dependencies,
    foreign_key_edges,
    infer_relationships,
    inspectable_artifacts,
    merge_registered,
    suggest_pii,
)
from lae.discovery.inference import candidate_pairs
from lae.discovery.pii import classify_column
from lae.lineage import build_lineage_graph

pytestmark = pytest.mark.unit


def _catalog(kit):
    return build_catalog(kit.demo_tables(), kit.demo_fks(), ["app"])


# ---------------------------------------------------------------------- artifacts


def test_views_and_matviews_are_auto_registered(kit):
    tables = [
        *kit.demo_tables(),
        kit.table("v_customer", [("id", "bigint")], kind=TableKind.VIEW, definition="SELECT 1"),
    ]
    cat = build_catalog(tables, [], ["app"])
    auto = artifacts_from_catalog(cat)
    assert [a.target.name for a in auto] == ["customer_ltv", "daily_revenue", "v_customer"]
    ltv = auto[0]
    assert ltv.kind is ArtifactKind.MATERIALIZED_VIEW
    assert ltv.refresh_strategy is RefreshStrategy.REFRESH_MATVIEW
    assert ltv.definition_sql == cat.table(kit.t("customer_ltv")).definition_sql
    assert ltv.artifact_id == auto_artifact_id(kit.t("customer_ltv"))
    assert ltv.name == "app.customer_ltv"
    assert auto[2].kind is ArtifactKind.VIEW and auto[2].refresh_strategy is RefreshStrategy.NONE
    assert artifacts_from_catalog(cat) == auto  # deterministic


def test_registered_artifacts_win(kit):
    cat = _catalog(kit)
    auto = artifacts_from_catalog(cat)
    registered = kit.demo_artifacts()
    merged = merge_registered(auto, registered)
    by_target = {a.target: a for a in merged}
    assert by_target[kit.t("customer_ltv")].artifact_id == kit.uid("customer_ltv")
    assert kit.t("customer_profile_cache") in by_target
    assert [a.target for a in merged] == sorted(by_target)


def test_external_artifacts_are_separated(kit):
    cat = _catalog(kit)
    ext = ArtifactDefinition(
        kit.uid("crm"),
        "crm",
        ArtifactKind.EXTERNAL,
        kit.t("crm", "external"),
        None,
        RefreshStrategy.EXTERNAL_TASK,
    )
    gone = ArtifactDefinition(
        kit.uid("gone"),
        "gone",
        ArtifactKind.DERIVED_TABLE,
        kit.t("gone"),
        "SELECT 1",
        RefreshStrategy.RECOMPUTE_SQL,
    )
    arts = [*kit.demo_artifacts(), ext, gone]
    assert [a.name for a in external_dependencies(arts, cat)] == ["gone", "crm"]  # by target
    assert {a.name for a in inspectable_artifacts(arts, cat)} == {
        a.name for a in kit.demo_artifacts()
    }


# ---------------------------------------------------------------------- inference


def _inference_setup(kit, mel_rows=None):
    cat = _catalog(kit)
    data = kit.demo_data()
    if mel_rows is not None:
        data[kit.t("marketing_event_log")] = mel_rows
    reader = kit.FakeCatalogReader(kit.demo_tables(), kit.demo_fks(), data)
    return cat, reader, foreign_key_edges(kit.demo_fks(), cat)


def test_candidates_skip_declared_fks(kit):
    cat, _, fk_edges = _inference_setup(kit)
    pairs = {(c.child.qualified(), c.parent.qualified()) for c in candidate_pairs(cat, fk_edges)}
    assert ("app.purchase_order.customer_id", "app.customer.id") not in pairs
    assert ("app.marketing_event_log.email", "app.customer.email") in pairs
    assert ("app.document_embedding.source_document_id", "app.document.id") in pairs
    everything = {(c.child.qualified(), c.parent.qualified()) for c in candidate_pairs(cat)}
    assert ("app.purchase_order.customer_id", "app.customer.id") in everything
    assert ("app.ticket_message.author_customer_id", "app.customer.id") in everything


def test_inferred_edges_are_suggested_with_confidence(kit):
    rows = [
        {"email": "five@example.org", "event": "open"},
        {"email": "bob@example.org", "event": "click"},
        {"email": "zed@example.org", "event": "open"},
    ]
    cat, reader, fk_edges = _inference_setup(kit, rows)
    edges = infer_relationships(cat, reader, exclude=fk_edges)
    assert edges, "expected the hidden e-mail copy to be suggested"
    for e in edges:
        assert e.origin is EdgeOrigin.INFERRED
        assert e.status is EdgeStatus.SUGGESTED
        assert 0 < e.confidence < 1
        assert e.details["sampled"] >= e.details["contained"] > 0
        assert set(e.details) == {"evidence", "pattern", "sampled", "contained"}
    mel = [e for e in edges if e.target == kit.t("marketing_event_log")]
    assert {e.level for e in mel} == {LineageLevel.TABLE, LineageLevel.COLUMN}
    assert mel[0].source == kit.t("customer") and mel[0].source_columns == ("email",)
    assert mel[0].confidence == 0.9  # ratio 1.0 x e-mail name weight 0.9
    emb = [e for e in edges if e.target == kit.t("document_embedding")]
    assert emb and emb[0].confidence == 0.95  # suffix match "document"
    # deterministic and valid as graph input; never traversed by default
    assert infer_relationships(cat, reader, exclude=fk_edges) == edges
    graph = build_lineage_graph(cat, [*fk_edges, *edges])
    assert all(e.origin is not EdgeOrigin.INFERRED for e in graph.downstream(kit.t("customer")))


def test_low_containment_is_not_suggested(kit):
    rows = [{"email": f"stranger{i}@example.org", "event": "x"} for i in range(10)] + [
        {"email": "bob@example.org", "event": "x"}
    ]
    cat, reader, fk_edges = _inference_setup(kit, rows)
    edges = infer_relationships(cat, reader, exclude=fk_edges)
    assert not [e for e in edges if e.target == kit.t("marketing_event_log")]
    lenient = infer_relationships(cat, reader, exclude=fk_edges, threshold=0.05)
    assert [e for e in lenient if e.target == kit.t("marketing_event_log")]


def test_empty_sample_is_skipped_and_sample_limit_passed(kit):
    cat, reader, fk_edges = _inference_setup(kit, [])
    edges = infer_relationships(cat, reader, exclude=fk_edges, sample=3)
    assert not [e for e in edges if e.target == kit.t("marketing_event_log")]
    assert all(c[0] in {"sample_values", "count_contained"} for c in reader.calls)


# ---------------------------------------------------------------------- PII


def test_suggest_pii(kit):
    cat = _catalog(kit)
    got = {p.column: p for p in suggest_pii(cat)}
    c = kit.t("customer")
    assert got[ColumnRef(c, "email")].category is PiiCategory.DIRECT_IDENTIFIER
    assert got[ColumnRef(c, "email")].strategy is ErasureStrategy.PSEUDONYMIZE  # NOT NULL
    assert got[ColumnRef(c, "phone")].strategy is ErasureStrategy.NULLIFY
    assert got[ColumnRef(c, "full_name")].category is PiiCategory.DIRECT_IDENTIFIER
    assert got[ColumnRef(kit.t("customer_address"), "street")].category is (
        PiiCategory.QUASI_IDENTIFIER
    )
    assert got[ColumnRef(kit.t("ticket_message"), "body")].category is PiiCategory.SENSITIVE
    assert got[ColumnRef(kit.t("document_embedding"), "embedding")].category is (
        PiiCategory.EMBEDDING
    )
    assert ColumnRef(c, "id") not in got
    assert ColumnRef(kit.t("product"), "name") not in got
    assert all(not p.is_confirmed and not p.use_for_residual_scan for p in got.values())
    # matview columns are derived, not suggested
    assert not [p for p in got if p.table == kit.t("customer_ltv")]
    assert list(got) == sorted(got)


@pytest.mark.parametrize(
    ("name", "dtype", "expected"),
    [
        ("contact_email", "text", PiiCategory.DIRECT_IDENTIFIER),
        ("mobile", "varchar(20)", PiiCategory.DIRECT_IDENTIFIER),
        ("email_count", "integer", None),
        ("zip_code", "text", PiiCategory.QUASI_IDENTIFIER),
        ("date_of_birth", "date", PiiCategory.QUASI_IDENTIFIER),
        ("notes", "text", PiiCategory.SENSITIVE),
        ("vec", "vector(384)", PiiCategory.EMBEDDING),
        ("status", "text", None),
    ],
)
def test_classify_column(name, dtype, expected):
    from lae.contracts.types import ColumnInfo

    assert classify_column(ColumnInfo(name, 1, dtype, True)) is expected
