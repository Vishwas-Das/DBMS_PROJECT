"""Inference on the real data and record-role edge cases built inside the rolled-back transaction.

Findings pinned here (docs/change_requests/lineage-discovery-engineer-2.md):
* §1: at the default threshold 0.95 the hidden copy marketing_event_log.email is NOT suggested,
  because `count_contained` compares exact text and the seeded case / whitespace variants of
  the subject's e-mail do not match. At 0.8 it is suggested (SUGGESTED, confidence < 1).
* §2: a message the subject wrote on an ANONYMOUS ticket (customer_id NULL) is SHARED (accepted
  CR-1 §6 rule: it references a ticket outside the subject's set). The generator's FORMAT /
  architecture §8.3 would call it OWNED. The seeded data never contains this case.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from sqlalchemy import text

from lae.contracts.enums import EdgeOrigin, EdgeStatus, LineageLevel, RecordRole
from lae.contracts.types import DiscoveryLimits, TableRef
from lae.db.catalog_queries import PgCatalogReader
from lae.discovery import discover_records, infer_relationships
from lae.lineage import build_lineage_graph

pytestmark = pytest.mark.integration

APP = "app"
TS = "2025-01-01T00:00:00Z"


def t(name: str) -> TableRef:
    return TableRef(APP, name)


def _known(pipeline: Any) -> list[Any]:
    return [*pipeline.fk_edges, *(e for r in pipeline.lineage for e in r.edges)]


def _table_edges(edges: list[Any]) -> dict[tuple[str, str], Any]:
    return {(e.source.name, e.target.name): e for e in edges if e.level is LineageLevel.TABLE}


def _discover(pipeline: Any, subject: int, graph: Any = None) -> Any:
    return discover_records(
        graph or pipeline.graph, pipeline.root, (subject,), pipeline.reader, DiscoveryLimits()
    )


def _roles(result: Any) -> dict[tuple[TableRef, tuple[Any, ...]], RecordRole]:
    return {(k.table, k.values): a.role for k, a in result.record_graph.nodes()}


def test_inference_suggests_only_and_skips_known_edges(pipeline: Any) -> None:
    reader = PgCatalogReader(pipeline.conn)
    edges = infer_relationships(pipeline.catalog, reader, exclude=_known(pipeline))
    assert edges, "expected at least one suggestion"
    for e in edges:
        assert e.origin is EdgeOrigin.INFERRED and e.status is EdgeStatus.SUGGESTED
        assert 0 < e.confidence < 1
        assert set(e.details) == {"evidence", "pattern", "sampled", "contained"}
    pairs = _table_edges(edges)
    # declared FKs and registered mappings are excluded
    assert ("document", "document_embedding") not in pairs
    assert ("customer", "customer_profile_cache") not in pairs
    assert ("customer", "purchase_order") not in pairs
    # CTAS keyed by customer_id but without an FK: suggested, containment 1 -> capped at 0.999
    seg = pairs[("customer", "customer_segment")]
    assert seg.confidence == 0.999
    assert seg.details["sampled"] == seg.details["contained"]
    # hidden copy NOT suggested at the default threshold (case variants break exact containment)
    assert ("customer", "marketing_event_log") not in pairs
    assert infer_relationships(pipeline.catalog, reader, exclude=_known(pipeline)) == edges


def test_hidden_copy_suggested_at_lower_threshold_and_never_traversed(pipeline: Any) -> None:
    reader = PgCatalogReader(pipeline.conn)
    edges = infer_relationships(pipeline.catalog, reader, threshold=0.8, exclude=_known(pipeline))
    mel = _table_edges(edges)[("customer", "marketing_event_log")]
    assert mel.status is EdgeStatus.SUGGESTED
    assert mel.source_columns == ("email",) and mel.target_columns == ("email",)
    assert mel.details["contained"] < mel.details["sampled"]
    assert mel.confidence < 0.9

    primary = pipeline.truth.meta["known_subjects"]["primary"]
    graph = build_lineage_graph(
        pipeline.catalog,
        [*_known(pipeline), *edges],
        artifacts=[a.target for a in pipeline.artifacts],
    )
    result = _discover(pipeline, primary, graph)
    assert t("marketing_event_log") not in {k.table for k, _ in result.record_graph.nodes()}
    assert any("unconfirmed table edge" in w for w in result.warnings)
    baseline = _discover(pipeline, primary)
    assert result.record_graph.record_set_hash() == baseline.record_graph.record_set_hash()


def test_confirmed_inferred_email_edge_finds_exact_copies_by_predicate(pipeline: Any) -> None:
    """Once a human confirms it, the e-mail edge is traversed (exact matches only); the predicate
    record is OWNED, never SHARED (CR-1 §6: e-mail edges do not create references)."""
    reader = PgCatalogReader(pipeline.conn)
    edges = infer_relationships(pipeline.catalog, reader, threshold=0.8, exclude=_known(pipeline))
    confirmed = [
        dataclasses.replace(e, status=EdgeStatus.CONFIRMED)
        if e.target == t("marketing_event_log")
        else e
        for e in edges
    ]
    graph = build_lineage_graph(
        pipeline.catalog,
        [*_known(pipeline), *confirmed],
        artifacts=[a.target for a in pipeline.artifacts],
    )
    primary = pipeline.truth.meta["known_subjects"]["primary"]
    result = _discover(pipeline, primary, graph)
    mel = [(k, a) for k, a in result.record_graph.nodes() if k.table == t("marketing_event_log")]
    assert len(mel) == 1
    key, attrs = mel[0]
    assert key.is_predicate and key.columns == ("email",)
    assert attrs.role is RecordRole.OWNED
    exact = pipeline.conn.execute(
        text(
            "SELECT count(*) FROM app.marketing_event_log m JOIN app.customer c "
            "ON c.email = m.email WHERE c.id = :id"
        ),
        {"id": primary},
    ).scalar_one()
    hidden = {h[2]: h[3] for h in pipeline.truth.subject(primary)["hidden_copies"]}
    assert exact == hidden["EXACT"] == 3  # the predicate covers 3 of the 5 copies
    assert hidden["LOWER_TRIM"] == 5  # the other 2 need the residual scan


def test_message_on_anonymous_ticket_is_shared_documented_divergence(pipeline: Any) -> None:
    subject = 9
    pipeline.conn.execute(
        text(
            "INSERT INTO app.support_ticket (id, customer_id, subject, status, opened_at) "
            "VALUES (9001, NULL, 'Synthetic', 'OPEN', :ts)"
        ),
        {"ts": TS},
    )
    pipeline.conn.execute(
        text(
            "INSERT INTO app.ticket_message (id, ticket_id, author_customer_id, body, sent_at) "
            "VALUES (9001, 9001, :c, 'Synthetic', :ts)"
        ),
        {"c": subject, "ts": TS},
    )
    roles = _roles(_discover(pipeline, subject))
    assert roles[(t("ticket_message"), (9001,))] is RecordRole.SHARED  # conservative (CR-2 §2)
    assert (t("support_ticket"), (9001,)) not in roles  # upstream ticket never becomes a record


def test_message_by_referred_customer_on_subject_ticket_is_shared(pipeline: Any) -> None:
    known = pipeline.truth.meta["known_subjects"]
    primary, referred = known["primary"], known["referred_by_primary"][0]
    ticket = pipeline.conn.execute(
        text("SELECT min(id) FROM app.support_ticket WHERE customer_id = :c"), {"c": primary}
    ).scalar_one()
    pipeline.conn.execute(
        text(
            "INSERT INTO app.ticket_message (id, ticket_id, author_customer_id, body, sent_at) "
            "VALUES (9002, :t, :a, 'Synthetic', :ts)"
        ),
        {"t": ticket, "a": referred, "ts": TS},
    )
    roles = _roles(_discover(pipeline, primary))
    assert roles[(t("ticket_message"), (9002,))] is RecordRole.SHARED
    assert roles[(t("customer"), (referred,))] is RecordRole.REFERENCING_OTHER_SUBJECT


def test_new_child_row_is_discovered_without_reseeding(pipeline: Any) -> None:
    """Records come only from queries: a row inserted in this transaction is found at once."""
    primary = pipeline.truth.meta["known_subjects"]["primary"]
    before = _discover(pipeline, primary)
    pipeline.conn.execute(
        text(
            "INSERT INTO app.customer_address (id, customer_id, line1, city, postal_code, "
            "country) VALUES (9003, :c, '1 Synthetic Way', 'Northmere', '00000', 'ZZ')"
        ),
        {"c": primary},
    )
    after = _discover(pipeline, primary)
    added = {k for k, _ in after.record_graph.nodes()} - {k for k, _ in before.record_graph.nodes()}
    assert [(k.table, k.values) for k in added] == [(t("customer_address"), (9003,))]
    assert before.record_graph.record_set_hash() != after.record_graph.record_set_hash()
