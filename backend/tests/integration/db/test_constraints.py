"""Every CHECK, unique / partial unique index, FK action and trigger rejects bad data.

Each case starts from a VALID row template (proved valid by `test_valid_templates_insert`),
applies one bad override and asserts the database rejects it with exactly the expected
constraint name. Everything runs in a rolled-back transaction.
"""

from __future__ import annotations

import itertools
import secrets
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, Table, insert, text
from sqlalchemy.exc import DBAPIError

from lae.db.models import Base

pytestmark = pytest.mark.integration

H = "a" * 64
_counter = itertools.count(1)


def hx() -> str:
    return secrets.token_hex(32)


def uniq(prefix: str = "t") -> str:
    return f"{prefix}_{next(_counter)}"


def table(name: str) -> Table:
    return Base.metadata.tables[f"lae.{name}"]


def ins(conn: Connection, name: str, values: dict[str, Any]) -> Any:
    t = table(name)
    pk = next(iter(t.primary_key.columns))
    return conn.execute(insert(t).values(**values).returning(pk)).scalar_one()


Graph = dict[str, Any]
Template = Callable[[Connection, Graph], dict[str, Any]]


def _plan_values(g: Graph, **over: Any) -> dict[str, Any]:
    v = {
        "request_id": g["request"],
        "snapshot_id": g["snapshot"],
        "version": next(_counter) + 100,
        "status": "SUPERSEDED",
        "schema_fingerprint": H,
        "record_set_hash": H,
        "plan_hash": H,
        "record_count": 0,
    }
    v.update(over)
    return v


TEMPLATES: dict[str, Template] = {
    "lineage_snapshot": lambda c, g: {
        "scanned_schemas": ["app"],
        "schema_fingerprint": H,
        "status": "READY",
    },
    "catalog_table": lambda c, g: {
        "snapshot_id": g["snapshot"],
        "schema_name": "app",
        "table_name": uniq(),
        "kind": "TABLE",
    },
    "catalog_column": lambda c, g: {
        "table_id": g["table"],
        "column_name": uniq("c"),
        "ordinal_position": next(_counter) + 10,
        "data_type": "text",
        "is_nullable": True,
    },
    "lineage_edge": lambda c, g: {
        "snapshot_id": g["snapshot"],
        "level": "TABLE",
        "origin": "DECLARED_FK",
        "source_table_id": g["table"],
        "target_table_id": g["table2"],
        "source_columns": ["id"],
        "target_columns": ["customer_id"],
        "transform": "FOREIGN_KEY",
        "traceability": "KEY_PRESERVING",
        "on_delete_action": "c",
        "status": "CONFIRMED",
    },
    "derived_artifact": lambda c, g: {
        "name": uniq("a"),
        "kind": "MATERIALIZED_VIEW",
        "schema_name": "app",
        "table_name": uniq("mv"),
        "definition_sql": "SELECT 1",
        "refresh_strategy": "REFRESH_MATVIEW",
    },
    "pii_classification": lambda c, g: {
        "schema_name": "app",
        "table_name": uniq(),
        "column_name": "email",
        "category": "DIRECT_IDENTIFIER",
        "erasure_strategy": "NULLIFY",
        "source": "MANUAL",
    },
    "subject_root": lambda c, g: {
        "name": uniq("root"),
        "schema_name": "app",
        "table_name": "customer",
        "key_columns": ["id"],
    },
    "retention_policy": lambda c, g: {
        "schema_name": "app",
        "table_name": uniq(),
        "action": "DELETE",
    },
    "erasure_request": lambda c, g: {
        "subject_root_id": g["root"],
        "subject_key": [1],
        "subject_key_hmac": H,
        "status": "RECEIVED",
        "requested_by": "tester",
    },
    "residual_probe": lambda c, g: {
        "request_id": g["request"],
        "origin_schema": "app",
        "origin_table": "customer",
        "origin_column": "email",
        "normalization": "LOWER_TRIM",
        "value_hmac": hx(),
    },
    "erasure_plan": lambda c, g: _plan_values(g),
    "plan_item": lambda c, g: {
        "plan_id": g["plan"],
        "step_order": next(_counter) + 10,
        "action": "DELETE_ROWS",
        "category": "DELETE",
        "reason": "test",
        "schema_name": "app",
        "table_name": "customer",
        "lineage_level": "RECORD",
        "expected_row_count": 1,
    },
    "plan_conflict": lambda c, g: {
        "plan_id": g["plan"],
        "code": "SHARED_RECORD",
        "severity": "BLOCKING",
        "schema_name": "app",
        "table_name": "ticket_message",
        "message": "shared",
    },
    "record_dependency": lambda c, g: {
        "plan_id": g["plan"],
        "parent_table": "app.customer",
        "child_table": "app.customer_address",
        "parent_key_hmac": H,
        "child_key_hmac": hx(),
        "child_role": "OWNED",
        "edge_id": g["edge"],
        "depth": 1,
    },
    "execution_run": lambda c, g: {
        "plan_id": g["plan"],
        "mode": "DRY_RUN",
        "status": "ROLLED_BACK",
    },
    "execution_step_result": lambda c, g: {
        "run_id": ins(
            c, "execution_run", {"plan_id": g["plan"], "mode": "DRY_RUN", "status": "ROLLED_BACK"}
        ),
        "plan_item_id": g["item"],
        "status": "APPLIED",
        "rows_affected": 1,
        "expected_rows": 1,
        "duration_ms": 3,
    },
    "verification_run": lambda c, g: {
        "execution_run_id": g["live_run"],
        "status": "PASSED",
        "outcome": "VERIFIED",
        "required_artifacts": 2,
        "verified_artifacts": 2,
    },
    "verification_check": lambda c, g: {
        "verification_run_id": g["vrun"],
        "check_type": "ROW_ABSENT",
        "passed": True,
        "observed": {"count": 0},
        "expected": {"count": 0},
    },
    "audit_event": lambda c, g: {
        "event_type": "PLAN_CREATED",
        "entity_type": "plan",
        "actor": "tester",
        "payload_canonical": "{}",
        "prev_hash": hx(),  # unique: migration 0003 forbids two successors of one event
        "event_hash": hx(),
    },
    "signing_key": lambda c, g: {
        "key_id": uniq("k"),
        "algorithm": "Ed25519",
        "public_key_pem": "-----BEGIN PUBLIC KEY-----",
    },
    "evidence_bundle": lambda c, g: {
        "plan_id": ins(c, "erasure_plan", _plan_values(g)),
        "verification_run_id": g["vrun"],
        "merkle_root": H,
        "leaf_count": 0,
        "leaves_canonical": "[]",
        "chain_anchor_seq": g["audit_seq"],
        "chain_anchor_hash": H,
        "bundle_canonical": "{}",
        "signature": "c2ln",
        "signing_key_id": g["key"],
    },
    "benchmark_run": lambda c, g: {
        "scenario": "B1",
        "params": {},
        "seed": 42,
        "environment": {},
        "metrics": {},
    },
}


@pytest.fixture
def graph(db_conn: Connection) -> Graph:
    """One valid row in every table, wired together."""
    c = db_conn
    g: Graph = {}
    g["snapshot"] = ins(c, "lineage_snapshot", TEMPLATES["lineage_snapshot"](c, g))
    g["table"] = ins(c, "catalog_table", TEMPLATES["catalog_table"](c, g))
    g["table2"] = ins(c, "catalog_table", TEMPLATES["catalog_table"](c, g))
    g["column"] = ins(c, "catalog_column", TEMPLATES["catalog_column"](c, g))
    g["edge"] = ins(c, "lineage_edge", TEMPLATES["lineage_edge"](c, g))
    g["artifact"] = ins(c, "derived_artifact", TEMPLATES["derived_artifact"](c, g))
    g["root"] = ins(c, "subject_root", TEMPLATES["subject_root"](c, g))
    g["request"] = ins(c, "erasure_request", TEMPLATES["erasure_request"](c, g))
    g["plan"] = ins(c, "erasure_plan", _plan_values(g, status="DRAFT", version=1))
    g["item"] = ins(c, "plan_item", TEMPLATES["plan_item"](c, g))
    g["live_run"] = ins(
        c, "execution_run", {"plan_id": g["plan"], "mode": "LIVE", "status": "COMMITTED"}
    )
    g["vrun"] = ins(c, "verification_run", TEMPLATES["verification_run"](c, g))
    g["audit_seq"] = ins(c, "audit_event", TEMPLATES["audit_event"](c, g))
    g["key"] = ins(c, "signing_key", TEMPLATES["signing_key"](c, g))
    return g


def constraint_violated(conn: Connection, fn: Callable[[], Any]) -> tuple[str | None, str | None]:
    """Run `fn` in a savepoint; return (sqlstate, constraint_name) of the error it raised."""
    sp = conn.begin_nested()
    try:
        fn()
    except DBAPIError as exc:
        sp.rollback()
        diag = getattr(exc.orig, "diag", None)
        return getattr(exc.orig, "sqlstate", None), getattr(diag, "constraint_name", None)
    sp.rollback()
    return None, None


def test_valid_templates_insert(db_conn: Connection, graph: Graph) -> None:
    for name, tmpl in TEMPLATES.items():
        values = tmpl(db_conn, graph)
        state, cname = constraint_violated(db_conn, lambda n=name, v=values: ins(db_conn, n, v))
        assert (state, cname) == (None, None), name


BAD = "x" * 64  # 64 chars but not hex

# (table, override, expected constraint name)
CASES: list[tuple[str, dict[str, Any], str]] = [
    ("lineage_snapshot", {"status": "NOPE"}, "ck_lineage_snapshot_status_valid"),
    ("lineage_snapshot", {"schema_fingerprint": BAD}, "ck_lineage_snapshot_schema_fingerprint_hex"),
    ("lineage_snapshot", {"scanned_schemas": []}, "ck_lineage_snapshot_schemas_nonempty"),
    ("lineage_snapshot", {"table_count": -1}, "ck_lineage_snapshot_table_count_nonneg"),
    ("lineage_snapshot", {"edge_count": -1}, "ck_lineage_snapshot_edge_count_nonneg"),
    ("catalog_table", {"kind": "SEQUENCE"}, "ck_catalog_table_kind_valid"),
    ("catalog_column", {"ordinal_position": 0}, "ck_catalog_column_ordinal_positive"),
    ("lineage_edge", {"level": "RECORD"}, "ck_lineage_edge_level_valid"),
    ("lineage_edge", {"origin": "GUESS"}, "ck_lineage_edge_origin_valid"),
    ("lineage_edge", {"transform": "MAGIC"}, "ck_lineage_edge_transform_valid"),
    ("lineage_edge", {"traceability": "MAYBE"}, "ck_lineage_edge_traceability_valid"),
    (
        "lineage_edge",
        {"status": "PENDING", "origin": "SQL_LINEAGE"},
        "ck_lineage_edge_status_valid",
    ),
    ("lineage_edge", {"on_delete_action": "x"}, "ck_lineage_edge_on_delete_action_valid"),
    ("lineage_edge", {"target_columns": ["a", "b"]}, "ck_lineage_edge_col_arity"),
    (
        "lineage_edge",
        {"level": "COLUMN", "source_columns": [], "target_columns": []},
        "ck_lineage_edge_column_has_cols",
    ),
    ("lineage_edge", {"on_delete_action": None}, "ck_lineage_edge_fk_confirmed"),
    ("lineage_edge", {"status": "SUGGESTED"}, "ck_lineage_edge_fk_confirmed"),
    (
        "lineage_edge",
        {"origin": "INFERRED", "status": "SUGGESTED", "confidence": 1, "on_delete_action": None},
        "ck_lineage_edge_inferred_confidence",
    ),
    ("lineage_edge", {"confidence": -0.5}, "ck_lineage_edge_confidence_range"),
    ("lineage_edge", {"confidence": 1.5}, "ck_lineage_edge_confidence_range"),
    ("derived_artifact", {"kind": "SPREADSHEET"}, "ck_derived_artifact_kind_valid"),
    (
        "derived_artifact",
        {"kind": "DERIVED_TABLE", "refresh_strategy": "PRAY"},
        "ck_derived_artifact_refresh_strategy_valid",
    ),
    (
        "derived_artifact",
        {"refresh_strategy": "RECOMPUTE_SQL"},
        "ck_derived_artifact_matview_strategy",
    ),
    (
        "derived_artifact",
        {"kind": "VIEW", "refresh_strategy": "REFRESH_MATVIEW"},
        "ck_derived_artifact_view_strategy",
    ),
    (
        "derived_artifact",
        {"kind": "EMBEDDING_TABLE", "refresh_strategy": "DELETE_BY_KEY", "definition_sql": None},
        "ck_derived_artifact_delete_by_key_mapping",
    ),
    (
        "derived_artifact",
        {"kind": "EMBEDDING_TABLE", "refresh_strategy": "RECOMPUTE_SQL", "definition_sql": None},
        "ck_derived_artifact_recompute_has_sql",
    ),
    (
        "derived_artifact",
        {"kind": "VIEW", "refresh_strategy": "NONE", "definition_sql": None},
        "ck_derived_artifact_definition_required",
    ),
    (
        "derived_artifact",
        {"kind": "EXTERNAL", "refresh_strategy": "EXTERNAL_TASK", "definition_sql": None},
        "ck_derived_artifact_external_opaque",
    ),
    ("pii_classification", {"category": "SECRET"}, "ck_pii_classification_category_valid"),
    (
        "pii_classification",
        {"erasure_strategy": "SHRED"},
        "ck_pii_classification_erasure_strategy_valid",
    ),
    ("pii_classification", {"source": "ORACLE"}, "ck_pii_classification_source_valid"),
    ("subject_root", {"key_columns": []}, "ck_subject_root_key_nonempty"),
    ("retention_policy", {"action": "KEEP"}, "ck_retention_policy_action_valid"),
    ("retention_policy", {"action": "RETAIN"}, "ck_retention_policy_legal_basis_required"),
    ("retention_policy", {"action": "ANONYMIZE"}, "ck_retention_policy_legal_basis_required"),
    ("erasure_request", {"status": "DONE"}, "ck_erasure_request_status_valid"),
    ("erasure_request", {"subject_key_hmac": BAD}, "ck_erasure_request_subject_key_hmac_hex"),
    ("erasure_request", {"subject_key": None}, "ck_erasure_request_subject_key_until_terminal"),
    (
        "erasure_request",
        {"subject_key": None, "status": "EXECUTED"},
        "ck_erasure_request_subject_key_until_terminal",
    ),
    ("residual_probe", {"normalization": "SOUNDEX"}, "ck_residual_probe_normalization_valid"),
    ("residual_probe", {"value_hmac": "A" * 64}, "ck_residual_probe_value_hmac_hex"),
    ("erasure_plan", {"status": "LOST"}, "ck_erasure_plan_status_valid"),
    ("erasure_plan", {"schema_fingerprint": BAD}, "ck_erasure_plan_schema_fingerprint_hex"),
    ("erasure_plan", {"record_set_hash": BAD}, "ck_erasure_plan_record_set_hash_hex"),
    ("erasure_plan", {"plan_hash": BAD}, "ck_erasure_plan_plan_hash_hex"),
    ("erasure_plan", {"approved_hash": BAD}, "ck_erasure_plan_approved_hash_hex"),
    ("erasure_plan", {"version": 0}, "ck_erasure_plan_version_positive"),
    ("erasure_plan", {"record_count": -1}, "ck_erasure_plan_record_count_nonneg"),
    ("erasure_plan", {"status": "EXECUTED"}, "ck_erasure_plan_approval_consistent"),
    (
        "erasure_plan",
        # migration 0002: SUPERSEDED may keep approval history; DRAFT/BLOCKED never may
        {"status": "BLOCKED", "approved_at": text("now()")},
        "ck_erasure_plan_approval_consistent",
    ),
    ("erasure_plan", {"approved_hash": "b" * 64}, "ck_erasure_plan_approved_hash_matches"),
    ("erasure_plan", {"status": "DRAFT"}, "uq_erasure_plan_active"),
    ("erasure_plan", {"status": "BLOCKED"}, "uq_erasure_plan_active"),
    ("plan_item", {"action": "BURN"}, "ck_plan_item_action_valid"),
    ("plan_item", {"category": "ERASE"}, "ck_plan_item_category_valid"),
    ("plan_item", {"lineage_level": "COLUMN"}, "ck_plan_item_lineage_level_valid"),
    ("plan_item", {"step_order": 0}, "ck_plan_item_step_order_positive"),
    ("plan_item", {"expected_row_count": -1}, "ck_plan_item_expected_rows_nonneg"),
    ("plan_item", {"expected_row_count": None}, "ck_plan_item_record_level_count"),
    ("plan_conflict", {"code": "OOPS"}, "ck_plan_conflict_code_valid"),
    ("plan_conflict", {"severity": "FATAL"}, "ck_plan_conflict_severity_valid"),
    ("plan_conflict", {"resolution": "IGNORED"}, "ck_plan_conflict_resolution_valid"),
    ("record_dependency", {"child_role": "OWNER"}, "ck_record_dependency_child_role_valid"),
    ("record_dependency", {"parent_key_hmac": BAD}, "ck_record_dependency_parent_key_hmac_hex"),
    ("record_dependency", {"child_key_hmac": BAD}, "ck_record_dependency_child_key_hmac_hex"),
    ("record_dependency", {"depth": -1}, "ck_record_dependency_depth_nonneg"),
    ("record_dependency", {"edge_id": None}, "ck_record_dependency_edge_required"),
    ("execution_run", {"mode": "MAYBE"}, "ck_execution_run_mode_valid"),
    ("execution_run", {"status": "DONE"}, "ck_execution_run_status_valid"),
    ("execution_run", {"status": "COMMITTED"}, "ck_execution_run_dry_run_never_commits"),
    (
        "execution_run",
        {"mode": "LIVE", "status": "COMMITTED"},
        "uq_execution_run_live_committed",
    ),
    ("execution_step_result", {"status": "OK"}, "ck_execution_step_result_status_valid"),
    ("execution_step_result", {"duration_ms": -1}, "ck_execution_step_result_duration_nonneg"),
    ("verification_run", {"status": "MEH"}, "ck_verification_run_status_valid"),
    ("verification_run", {"outcome": "MOSTLY"}, "ck_verification_run_outcome_valid"),
    ("verification_run", {"verified_artifacts": 3}, "ck_verification_run_coverage_bounded"),
    ("verification_check", {"check_type": "VIBES"}, "ck_verification_check_check_type_valid"),
    ("audit_event", {"event_type": "HACKED"}, "ck_audit_event_event_type_valid"),
    ("audit_event", {"prev_hash": BAD}, "ck_audit_event_prev_hash_hex"),
    ("audit_event", {"event_hash": BAD}, "ck_audit_event_event_hash_hex"),
    ("signing_key", {"algorithm": "RSA"}, "ck_signing_key_algorithm_valid"),
    ("evidence_bundle", {"merkle_root": BAD}, "ck_evidence_bundle_merkle_root_hex"),
    ("evidence_bundle", {"chain_anchor_hash": BAD}, "ck_evidence_bundle_chain_anchor_hash_hex"),
    ("evidence_bundle", {"leaf_count": -1}, "ck_evidence_bundle_leaf_count_nonneg"),
    ("benchmark_run", {"scenario": "X1"}, "ck_benchmark_run_scenario_format"),
]


@pytest.mark.parametrize(("table_name", "override", "expected"), CASES, ids=[c[2] for c in CASES])
def test_check_rejects(
    db_conn: Connection, graph: Graph, table_name: str, override: dict[str, Any], expected: str
) -> None:
    values = TEMPLATES[table_name](db_conn, graph)
    values.update(override)
    state, cname = constraint_violated(db_conn, lambda: ins(db_conn, table_name, values))
    assert cname == expected
    assert state in {"23514", "23505"}


# (table, column overrides copied from an existing graph row, expected unique name)
UNIQUE_CASES: list[tuple[str, Callable[[Connection, Graph], dict[str, Any]], str]] = [
    (  # migration 0003: the audit chain may never fork
        "audit_event",
        lambda c, g: {"prev_hash": _col(c, "audit_event", "prev_hash", g["audit_seq"])},
        "uq_audit_event_prev_hash",
    ),
    (
        "catalog_table",
        lambda c, g: {"table_name": _col(c, "catalog_table", "table_name", g["table"])},
        "uq_catalog_table_snapshot_id_schema_name_table_name",
    ),
    (
        "catalog_column",
        lambda c, g: {"column_name": _col(c, "catalog_column", "column_name", g["column"])},
        "uq_catalog_column_table_id_column_name",
    ),
    (
        "catalog_column",
        lambda c, g: {
            "ordinal_position": _col(c, "catalog_column", "ordinal_position", g["column"])
        },
        "uq_catalog_column_table_id_ordinal_position",
    ),
    (
        "derived_artifact",
        lambda c, g: {"name": _col(c, "derived_artifact", "name", g["artifact"])},
        "uq_derived_artifact_name",
    ),
    (
        "derived_artifact",
        lambda c, g: {"table_name": _col(c, "derived_artifact", "table_name", g["artifact"])},
        "uq_derived_artifact_schema_name_table_name",
    ),
    (
        "subject_root",
        lambda c, g: {"name": _col(c, "subject_root", "name", g["root"])},
        "uq_subject_root_name",
    ),
    (
        "erasure_plan",
        lambda c, g: {"version": 1},
        "uq_erasure_plan_request_id_version",
    ),
    (
        "plan_item",
        lambda c, g: {"step_order": _col(c, "plan_item", "step_order", g["item"])},
        "uq_plan_item_plan_id_step_order",
    ),
    (
        "execution_step_result",
        lambda c, g: _existing_step(c, g),
        "uq_execution_step_result_run_id_plan_item_id",
    ),
    (
        "audit_event",
        lambda c, g: {"event_hash": _audit_hash(c, g)},
        "uq_audit_event_event_hash",
    ),
    (
        "evidence_bundle",
        lambda c, g: {"plan_id": g["plan"]},
        "uq_evidence_bundle_plan_id",
    ),
]


def _col(conn: Connection, name: str, column: str, pk: Any) -> Any:
    t = table(name)
    pkcol = next(iter(t.primary_key.columns))
    return conn.execute(t.select().with_only_columns(t.c[column]).where(pkcol == pk)).scalar_one()


def _existing_step(conn: Connection, g: Graph) -> dict[str, Any]:
    vals = dict(TEMPLATES["execution_step_result"](conn, g), run_id=g["live_run"])
    ins(conn, "execution_step_result", vals)
    return {"run_id": g["live_run"], "plan_item_id": g["item"]}


def _audit_hash(conn: Connection, g: Graph) -> str:
    return str(_col(conn, "audit_event", "event_hash", g["audit_seq"]))


@pytest.mark.parametrize(
    ("table_name", "dup", "expected"), UNIQUE_CASES, ids=[c[2] for c in UNIQUE_CASES]
)
def test_unique_rejects(
    db_conn: Connection,
    graph: Graph,
    table_name: str,
    dup: Callable[[Connection, Graph], dict[str, Any]],
    expected: str,
) -> None:
    values = TEMPLATES[table_name](db_conn, graph)
    if table_name == "evidence_bundle":
        ins(db_conn, "evidence_bundle", dict(values, plan_id=graph["plan"]))
        values = TEMPLATES[table_name](db_conn, graph)
    values.update(dup(db_conn, graph))
    state, cname = constraint_violated(db_conn, lambda: ins(db_conn, table_name, values))
    assert (state, cname) == ("23505", expected)


def test_pii_and_retention_and_probe_uniques(db_conn: Connection, graph: Graph) -> None:
    for name, expected in (
        ("pii_classification", "uq_pii_classification_schema_name_table_name_column_name"),
        ("retention_policy", "uq_retention_policy_schema_name_table_name"),
        ("residual_probe", "uq_residual_probe_request_id_value_hmac_normalization"),
    ):
        values = TEMPLATES[name](db_conn, graph)
        ins(db_conn, name, values)
        state, cname = constraint_violated(db_conn, lambda n=name, v=values: ins(db_conn, n, v))
        assert (state, cname) == ("23505", expected)


def test_only_one_live_plan_but_superseded_allowed(db_conn: Connection, graph: Graph) -> None:
    # DRAFT exists in the graph; an APPROVED second live plan is rejected ...
    approved = _plan_values(
        graph, status="APPROVED", approved_at=text("now()"), approved_hash=H, approved_by="x"
    )
    assert constraint_violated(db_conn, lambda: ins(db_conn, "erasure_plan", approved))[1] == (
        "uq_erasure_plan_active"
    )
    # ... but any number of non-live plans are fine.
    for status in ("SUPERSEDED", "FAILED"):
        ins(db_conn, "erasure_plan", _plan_values(graph, status=status))


# ------------------------------------------------------------------ foreign-key actions


def test_fk_restrict_and_cascade(db_conn: Connection, graph: Graph) -> None:
    c = db_conn
    # erasure_request -> subject_root ON DELETE RESTRICT
    state, cname = constraint_violated(
        c,
        lambda: c.execute(text("DELETE FROM lae.subject_root WHERE id = :i"), {"i": graph["root"]}),
    )
    assert (state, cname) == ("23503", "fk_erasure_request_subject_root_id_subject_root")
    # execution_run -> erasure_plan ON DELETE RESTRICT
    state, cname = constraint_violated(
        c,
        lambda: c.execute(text("DELETE FROM lae.erasure_plan WHERE id = :i"), {"i": graph["plan"]}),
    )
    assert (state, cname) == ("23503", "fk_execution_run_plan_id_erasure_plan")
    # lineage_edge.artifact_id ON DELETE SET NULL
    edge = ins(
        c, "lineage_edge", dict(TEMPLATES["lineage_edge"](c, graph), artifact_id=graph["artifact"])
    )
    c.execute(text("DELETE FROM lae.derived_artifact WHERE id = :i"), {"i": graph["artifact"]})
    assert _col(c, "lineage_edge", "artifact_id", edge) is None
    # residual_probe -> erasure_request ON DELETE CASCADE (on a request without plans)
    req = ins(c, "erasure_request", TEMPLATES["erasure_request"](c, graph))
    ins(c, "residual_probe", dict(TEMPLATES["residual_probe"](c, graph), request_id=req))
    c.execute(text("DELETE FROM lae.erasure_request WHERE id = :i"), {"i": req})
    left = c.execute(
        text("SELECT count(*) FROM lae.residual_probe WHERE request_id = :i"), {"i": req}
    ).scalar_one()
    assert left == 0


# ------------------------------------------------------------------ triggers


def test_verification_run_requires_live_committed_execution(
    db_conn: Connection, graph: Graph
) -> None:
    for mode, status in (("DRY_RUN", "ROLLED_BACK"), ("LIVE", "RUNNING"), ("LIVE", "FAILED")):
        run = ins(
            db_conn, "execution_run", {"plan_id": graph["plan"], "mode": mode, "status": status}
        )
        values = dict(TEMPLATES["verification_run"](db_conn, graph), execution_run_id=run)
        state, cname = constraint_violated(
            db_conn, lambda v=values: ins(db_conn, "verification_run", v)
        )
        assert (state, cname) == ("23514", "trg_verification_run_requires_committed"), (
            mode,
            status,
        )
    # re-pointing an existing verification run to a non-committed execution is rejected too
    dry = ins(
        db_conn, "execution_run", {"plan_id": graph["plan"], "mode": "DRY_RUN", "status": "FAILED"}
    )
    state, cname = constraint_violated(
        db_conn,
        lambda: db_conn.execute(
            text("UPDATE lae.verification_run SET execution_run_id = :e WHERE id = :i"),
            {"e": dry, "i": graph["vrun"]},
        ),
    )
    assert cname == "trg_verification_run_requires_committed"


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE lae.audit_event SET actor = 'mallory' WHERE seq = :seq",
        "UPDATE lae.audit_event SET payload_canonical = '{}' WHERE seq = :seq",
        "DELETE FROM lae.audit_event WHERE seq = :seq",
        "TRUNCATE lae.audit_event CASCADE",
        "TRUNCATE lae.audit_event, lae.evidence_bundle",
    ],
)
def test_audit_event_is_append_only(db_conn: Connection, graph: Graph, statement: str) -> None:
    sp = db_conn.begin_nested()
    with pytest.raises(DBAPIError) as info:
        db_conn.execute(text(statement), {"seq": graph["audit_seq"]})
    sp.rollback()
    assert getattr(info.value.orig, "sqlstate", None) == "42501"
    assert "append-only" in str(info.value.orig)
    still = db_conn.execute(
        text("SELECT count(*) FROM lae.audit_event WHERE seq = :s"), {"s": graph["audit_seq"]}
    ).scalar_one()
    assert still == 1


def test_audit_event_seq_is_generated_always(db_conn: Connection, graph: Graph) -> None:
    values = dict(TEMPLATES["audit_event"](db_conn, graph), seq=10**9)
    state, _ = constraint_violated(db_conn, lambda: ins(db_conn, "audit_event", values))
    assert state == "428C9"  # cannot insert a non-DEFAULT value into a GENERATED ALWAYS column


def test_uuid_defaults(db_conn: Connection, graph: Graph) -> None:
    assert isinstance(graph["snapshot"], UUID)
    assert graph["snapshot"] != uuid4()
