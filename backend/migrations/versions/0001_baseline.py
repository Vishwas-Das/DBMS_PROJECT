"""Baseline: every `lae` table, constraint, index, trigger, role and grant.

Contract: docs/database_schema.md §1, §3 and §7 (CR lead-1). Enumeration CHECK lists are built
from lae.contracts.enums so the database and the Python StrEnums cannot drift apart. Constraint
names are spelled out explicitly (op.f) and follow the naming convention of §1; the integration
test `test_metadata_parity` proves they are identical to lae.db.models.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-11-04
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from enum import StrEnum

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from lae.contracts.enums import (
    ActionCategory,
    ArtifactKind,
    AuditEventType,
    CheckType,
    ConflictCode,
    EdgeOrigin,
    EdgeStatus,
    ErasureStrategy,
    ExecutionMode,
    ExecutionStatus,
    LineageLevel,
    Normalization,
    OnDeleteAction,
    PiiCategory,
    PiiSource,
    PlanAction,
    PlanStatus,
    RecordRole,
    RefreshStrategy,
    RequestStatus,
    RetentionAction,
    Severity,
    SnapshotStatus,
    StepStatus,
    TableKind,
    Traceability,
    Transform,
    VerificationOutcome,
    VerificationStatus,
)

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "lae"
HEX64 = "~ '^[0-9a-f]{64}$'"
READER_ROLE = "lae_reader"

# Tables in creation order (downgrade drops them in reverse).
TABLES = (
    "lineage_snapshot",
    "catalog_table",
    "catalog_column",
    "derived_artifact",
    "lineage_edge",
    "pii_classification",
    "subject_root",
    "retention_policy",
    "erasure_request",
    "residual_probe",
    "erasure_plan",
    "plan_item",
    "plan_conflict",
    "record_dependency",
    "execution_run",
    "execution_step_result",
    "verification_run",
    "verification_check",
    "audit_event",
    "signing_key",
    "evidence_bundle",
    "benchmark_run",
)


# ------------------------------------------------------------------ helpers (names per §1)


def _in(values: Iterable[str]) -> str:
    vals = [str(v) for v in values]
    for v in vals:
        if not v.replace("_", "").isalnum():
            raise ValueError(f"enum literal {v!r} is not alphanumeric")
    return ", ".join(f"'{v}'" for v in vals)


def _vals(enum: type[StrEnum] | Iterable[StrEnum]) -> list[str]:
    return [e.value for e in enum]


def ck(table: str, name: str, expr: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expr, name=op.f(f"ck_{table}_{name}"))


def ck_enum(
    table: str, col: str, enum: type[StrEnum] | Iterable[StrEnum], *, nullable: bool = False
) -> sa.CheckConstraint:
    expr = f"{col} IN ({_in(_vals(enum))})"
    if nullable:
        expr = f"{col} IS NULL OR {expr}"
    return ck(table, f"{col}_valid", expr)


def ck_hex(table: str, col: str, *, nullable: bool = False) -> sa.CheckConstraint:
    expr = f"{col} {HEX64}"
    if nullable:
        expr = f"{col} IS NULL OR {expr}"
    return ck(table, f"{col}_hex", expr)


def pk(table: str, *cols: str) -> sa.PrimaryKeyConstraint:
    return sa.PrimaryKeyConstraint(*cols, name=op.f(f"pk_{table}"))


def uq(table: str, *cols: str) -> sa.UniqueConstraint:
    return sa.UniqueConstraint(*cols, name=op.f(f"uq_{table}_{'_'.join(cols)}"))


def fk(
    table: str, col: str, ref_table: str, ref_col: str = "id", *, ondelete: str | None = None
) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [col],
        [f"{S}.{ref_table}.{ref_col}"],
        name=op.f(f"fk_{table}_{col}_{ref_table}"),
        ondelete=ondelete,
    )


def ix(table: str, *cols: str) -> None:
    op.create_index(op.f(f"ix_{table}_{'_'.join(cols)}"), table, list(cols), schema=S)


def uuid_pk() -> sa.Column[object]:
    return sa.Column(
        "id", pg.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False
    )


def uuid_col(name: str, *, nullable: bool = False) -> sa.Column[object]:
    return sa.Column(name, pg.UUID(as_uuid=True), nullable=nullable)


def ts(name: str, *, nullable: bool = False, now: bool = True) -> sa.Column[object]:
    return sa.Column(
        name,
        pg.TIMESTAMP(timezone=True),
        server_default=sa.text("now()") if now else None,
        nullable=nullable,
    )


def text_col(name: str, *, nullable: bool = False) -> sa.Column[object]:
    return sa.Column(name, sa.Text(), nullable=nullable)


def hex_col(name: str, *, nullable: bool = False) -> sa.Column[object]:
    return sa.Column(name, sa.CHAR(64), nullable=nullable)


def text_array(name: str, *, default_empty: bool = True) -> sa.Column[object]:
    return sa.Column(
        name,
        pg.ARRAY(sa.Text()),
        server_default=sa.text("'{}'::text[]") if default_empty else None,
        nullable=False,
    )


def jsonb(name: str, *, nullable: bool = False, default: str | None = None) -> sa.Column[object]:
    return sa.Column(
        name,
        pg.JSONB(astext_type=sa.Text()),
        server_default=sa.text(default) if default else None,
        nullable=nullable,
    )


def boolean(name: str, default: bool | None = None) -> sa.Column[object]:
    return sa.Column(
        name,
        sa.Boolean(),
        server_default=None if default is None else sa.text("true" if default else "false"),
        nullable=False,
    )


# ------------------------------------------------------------------ trigger / role DDL

AUDIT_APPEND_ONLY_FN = """
CREATE FUNCTION lae.audit_event_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'lae.audit_event is append-only: % is not allowed', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END
$$
"""

VERIFICATION_REQUIRES_COMMITTED_FN = """
CREATE FUNCTION lae.verification_run_requires_committed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM lae.execution_run r
        WHERE r.id = NEW.execution_run_id AND r.mode = 'LIVE' AND r.status = 'COMMITTED'
    ) THEN
        RAISE EXCEPTION 'verification_run requires a LIVE, COMMITTED execution_run'
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'trg_verification_run_requires_committed';
    END IF;
    RETURN NEW;
END
$$
"""

ENSURE_READER_ROLE = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lae_reader') THEN
        CREATE ROLE lae_reader NOLOGIN;
    END IF;
END
$$
"""


# ------------------------------------------------------------------ upgrade


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE SCHEMA IF NOT EXISTS lae")

    _catalog_tables()
    _config_tables()
    _workflow_tables()
    _audit_tables()
    _triggers()
    _grants()


def _catalog_tables() -> None:
    t = "lineage_snapshot"
    op.create_table(
        t,
        uuid_pk(),
        ts("created_at"),
        sa.Column("scanned_schemas", pg.ARRAY(sa.Text()), nullable=False),
        hex_col("schema_fingerprint"),
        text_col("status"),
        sa.Column("table_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("edge_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        jsonb("warnings", default="'[]'::jsonb"),
        pk(t, "id"),
        ck_enum(t, "status", SnapshotStatus),
        ck_hex(t, "schema_fingerprint"),
        ck(t, "schemas_nonempty", "cardinality(scanned_schemas) > 0"),
        ck(t, "table_count_nonneg", "table_count >= 0"),
        ck(t, "edge_count_nonneg", "edge_count >= 0"),
        schema=S,
    )
    op.create_index(
        op.f("ix_lineage_snapshot_created_at"),
        t,
        [sa.text("created_at DESC")],
        schema=S,
    )

    t = "catalog_table"
    op.create_table(
        t,
        uuid_pk(),
        uuid_col("snapshot_id"),
        text_col("schema_name"),
        text_col("table_name"),
        text_col("kind"),
        text_array("primary_key_columns"),
        sa.Column("estimated_rows", sa.BigInteger(), nullable=True),
        text_col("definition_sql", nullable=True),
        pk(t, "id"),
        uq(t, "snapshot_id", "schema_name", "table_name"),
        ck_enum(t, "kind", TableKind),
        fk(t, "snapshot_id", "lineage_snapshot", ondelete="CASCADE"),
        schema=S,
    )

    t = "catalog_column"
    op.create_table(
        t,
        uuid_pk(),
        uuid_col("table_id"),
        text_col("column_name"),
        sa.Column("ordinal_position", sa.SmallInteger(), nullable=False),
        text_col("data_type"),
        sa.Column("is_nullable", sa.Boolean(), nullable=False),
        pk(t, "id"),
        uq(t, "table_id", "column_name"),
        uq(t, "table_id", "ordinal_position"),
        ck(t, "ordinal_positive", "ordinal_position > 0"),
        fk(t, "table_id", "catalog_table", ondelete="CASCADE"),
        schema=S,
    )


def _config_tables() -> None:
    t = "derived_artifact"
    op.create_table(
        t,
        uuid_pk(),
        text_col("name"),
        text_col("kind"),
        text_col("schema_name"),
        text_col("table_name"),
        text_col("definition_sql", nullable=True),
        text_col("refresh_strategy"),
        jsonb("source_key_mapping", nullable=True),
        boolean("is_auto_registered", default=False),
        boolean("is_active", default=True),
        boolean("is_inspectable", default=True),
        ts("created_at"),
        ts("updated_at"),
        pk(t, "id"),
        uq(t, "name"),
        uq(t, "schema_name", "table_name"),
        ck_enum(t, "kind", ArtifactKind),
        ck_enum(t, "refresh_strategy", RefreshStrategy),
        ck(
            t,
            "matview_strategy",
            "kind <> 'MATERIALIZED_VIEW' OR refresh_strategy IN ('REFRESH_MATVIEW', 'NONE')",
        ),
        ck(t, "view_strategy", "kind <> 'VIEW' OR refresh_strategy = 'NONE'"),
        ck(
            t,
            "delete_by_key_mapping",
            "refresh_strategy <> 'DELETE_BY_KEY' OR source_key_mapping IS NOT NULL",
        ),
        ck(
            t,
            "recompute_has_sql",
            "refresh_strategy <> 'RECOMPUTE_SQL' OR definition_sql IS NOT NULL",
        ),
        ck(
            t,
            "definition_required",
            "definition_sql IS NOT NULL OR kind IN ('EMBEDDING_TABLE', 'CACHE', 'EXTERNAL')",
        ),
        ck(t, "external_opaque", "kind <> 'EXTERNAL' OR is_inspectable = false"),
        schema=S,
    )

    t = "lineage_edge"
    op.create_table(
        t,
        uuid_pk(),
        uuid_col("snapshot_id"),
        text_col("level"),
        text_col("origin"),
        uuid_col("source_table_id"),
        uuid_col("target_table_id"),
        text_array("source_columns"),
        text_array("target_columns"),
        text_col("transform"),
        text_col("traceability"),
        sa.Column("on_delete_action", sa.CHAR(1), nullable=True),
        text_col("constraint_name", nullable=True),
        sa.Column("confidence", sa.Numeric(4, 3), server_default=sa.text("1"), nullable=False),
        text_col("status"),
        uuid_col("artifact_id", nullable=True),
        jsonb("details", default="'{}'::jsonb"),
        pk(t, "id"),
        ck_enum(t, "level", (LineageLevel.TABLE, LineageLevel.COLUMN)),
        ck_enum(t, "origin", EdgeOrigin),
        ck_enum(t, "transform", Transform),
        ck_enum(t, "traceability", Traceability),
        ck_enum(t, "status", EdgeStatus),
        ck_enum(t, "on_delete_action", OnDeleteAction, nullable=True),
        ck(t, "col_arity", "cardinality(source_columns) = cardinality(target_columns)"),
        ck(
            t,
            "column_has_cols",
            "level <> 'COLUMN' OR (cardinality(source_columns) >= 1 "
            "AND cardinality(target_columns) >= 1)",
        ),
        ck(
            t,
            "fk_confirmed",
            "origin <> 'DECLARED_FK' OR (on_delete_action IS NOT NULL AND status = 'CONFIRMED')",
        ),
        ck(
            t,
            "inferred_confidence",
            "origin <> 'INFERRED' OR confidence < 1 OR status <> 'SUGGESTED'",
        ),
        ck(t, "confidence_range", "confidence BETWEEN 0 AND 1"),
        fk(t, "snapshot_id", "lineage_snapshot", ondelete="CASCADE"),
        fk(t, "source_table_id", "catalog_table", ondelete="CASCADE"),
        fk(t, "target_table_id", "catalog_table", ondelete="CASCADE"),
        fk(t, "artifact_id", "derived_artifact", ondelete="SET NULL"),
        schema=S,
    )
    ix(t, "snapshot_id", "source_table_id", "level")
    ix(t, "snapshot_id", "target_table_id", "level")
    ix(t, "snapshot_id", "status")

    t = "pii_classification"
    op.create_table(
        t,
        uuid_pk(),
        text_col("schema_name"),
        text_col("table_name"),
        text_col("column_name"),
        text_col("category"),
        text_col("erasure_strategy"),
        text_col("source"),
        boolean("is_confirmed", default=False),
        boolean("use_for_residual_scan", default=False),
        pk(t, "id"),
        uq(t, "schema_name", "table_name", "column_name"),
        ck_enum(t, "category", PiiCategory),
        ck_enum(t, "erasure_strategy", ErasureStrategy),
        ck_enum(t, "source", PiiSource),
        schema=S,
    )

    t = "subject_root"
    op.create_table(
        t,
        uuid_pk(),
        text_col("name"),
        text_col("schema_name"),
        text_col("table_name"),
        text_array("key_columns", default_empty=False),
        text_array("identifier_columns"),
        pk(t, "id"),
        uq(t, "name"),
        ck(t, "key_nonempty", "cardinality(key_columns) >= 1"),
        schema=S,
    )

    t = "retention_policy"
    op.create_table(
        t,
        uuid_pk(),
        text_col("schema_name"),
        text_col("table_name"),
        text_col("action"),
        text_col("legal_basis", nullable=True),
        pk(t, "id"),
        uq(t, "schema_name", "table_name"),
        ck_enum(t, "action", RetentionAction),
        ck(t, "legal_basis_required", "action = 'DELETE' OR legal_basis IS NOT NULL"),
        schema=S,
    )


def _workflow_tables() -> None:
    t = "erasure_request"
    op.create_table(
        t,
        uuid_pk(),
        uuid_col("subject_root_id"),
        jsonb("subject_key", nullable=True),
        hex_col("subject_key_hmac"),
        text_col("status"),
        text_col("requested_by"),
        text_col("reason", nullable=True),
        ts("created_at"),
        ts("completed_at", nullable=True, now=False),
        pk(t, "id"),
        ck_enum(t, "status", RequestStatus),
        ck_hex(t, "subject_key_hmac"),
        ck(
            t,
            "subject_key_until_terminal",
            "status IN ('VERIFIED', 'CANCELLED') OR subject_key IS NOT NULL",
        ),
        fk(t, "subject_root_id", "subject_root", ondelete="RESTRICT"),
        schema=S,
    )
    ix(t, "subject_root_id", "subject_key_hmac")

    t = "residual_probe"
    op.create_table(
        t,
        uuid_pk(),
        uuid_col("request_id"),
        text_col("origin_schema"),
        text_col("origin_table"),
        text_col("origin_column"),
        text_col("normalization"),
        hex_col("value_hmac"),
        pk(t, "id"),
        uq(t, "request_id", "value_hmac", "normalization"),
        ck_enum(t, "normalization", Normalization),
        ck_hex(t, "value_hmac"),
        fk(t, "request_id", "erasure_request", ondelete="CASCADE"),
        schema=S,
    )

    approved = (PlanStatus.APPROVED, PlanStatus.EXECUTED, PlanStatus.VERIFIED)
    live = (PlanStatus.DRAFT, PlanStatus.BLOCKED, PlanStatus.APPROVED)
    t = "erasure_plan"
    op.create_table(
        t,
        uuid_pk(),
        uuid_col("request_id"),
        uuid_col("snapshot_id"),
        sa.Column("version", sa.Integer(), nullable=False),
        text_col("status"),
        hex_col("schema_fingerprint"),
        hex_col("record_set_hash"),
        hex_col("plan_hash"),
        sa.Column("record_count", sa.Integer(), nullable=False),
        ts("created_at"),
        text_col("approved_by", nullable=True),
        ts("approved_at", nullable=True, now=False),
        hex_col("approved_hash", nullable=True),
        pk(t, "id"),
        uq(t, "request_id", "version"),
        ck_enum(t, "status", PlanStatus),
        ck_hex(t, "schema_fingerprint"),
        ck_hex(t, "record_set_hash"),
        ck_hex(t, "plan_hash"),
        ck_hex(t, "approved_hash", nullable=True),
        ck(t, "version_positive", "version >= 1"),
        ck(t, "record_count_nonneg", "record_count >= 0"),
        ck(
            t,
            "approval_consistent",
            f"(status IN ({_in(_vals(approved))})) = (approved_at IS NOT NULL)",
        ),
        ck(t, "approved_hash_matches", "approved_hash IS NULL OR approved_hash = plan_hash"),
        fk(t, "request_id", "erasure_request", ondelete="RESTRICT"),
        fk(t, "snapshot_id", "lineage_snapshot", ondelete="RESTRICT"),
        schema=S,
    )
    op.create_index(
        op.f("uq_erasure_plan_active"),
        t,
        ["request_id"],
        unique=True,
        schema=S,
        postgresql_where=sa.text(f"status IN ({_in(_vals(live))})"),
    )

    t = "plan_item"
    op.create_table(
        t,
        uuid_pk(),
        uuid_col("plan_id"),
        sa.Column("step_order", sa.Integer(), nullable=False),
        text_col("action"),
        text_col("category"),
        text_col("reason"),
        text_col("schema_name"),
        text_col("table_name"),
        text_col("lineage_level"),
        text_array("key_columns"),
        jsonb("record_keys", nullable=True),
        text_array("record_key_hmacs"),
        text_array("target_columns"),
        sa.Column("expected_row_count", sa.Integer(), nullable=True),
        sa.Column(
            "depends_on_steps",
            pg.ARRAY(sa.Integer()),
            server_default=sa.text("'{}'::integer[]"),
            nullable=False,
        ),
        sa.Column(
            "reason_edge_ids",
            pg.ARRAY(pg.UUID(as_uuid=True)),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        pk(t, "id"),
        uq(t, "plan_id", "step_order"),
        ck_enum(t, "action", PlanAction),
        ck_enum(t, "category", ActionCategory),
        ck_enum(t, "lineage_level", (LineageLevel.RECORD, LineageLevel.TABLE)),
        ck(t, "step_order_positive", "step_order >= 1"),
        ck(t, "expected_rows_nonneg", "expected_row_count IS NULL OR expected_row_count >= 0"),
        ck(t, "record_level_count", "lineage_level <> 'RECORD' OR expected_row_count IS NOT NULL"),
        fk(t, "plan_id", "erasure_plan", ondelete="CASCADE"),
        schema=S,
    )

    t = "plan_conflict"
    op.create_table(
        t,
        uuid_pk(),
        uuid_col("plan_id"),
        text_col("code"),
        text_col("severity"),
        text_col("schema_name"),
        text_col("table_name"),
        text_col("message"),
        text_col("resolution", nullable=True),
        text_col("resolved_by", nullable=True),
        pk(t, "id"),
        ck_enum(t, "code", ConflictCode),
        ck_enum(t, "severity", Severity),
        ck(
            t,
            "resolution_valid",
            f"resolution IS NULL OR resolution IN ({_in(('ACKNOWLEDGED', 'POLICY_CHANGED'))})",
        ),
        fk(t, "plan_id", "erasure_plan", ondelete="CASCADE"),
        schema=S,
    )

    t = "record_dependency"
    op.create_table(
        t,
        uuid_pk(),
        uuid_col("plan_id"),
        text_col("parent_table"),
        text_col("child_table"),
        jsonb("parent_key", nullable=True),
        jsonb("child_key", nullable=True),
        hex_col("parent_key_hmac"),
        hex_col("child_key_hmac"),
        text_col("child_role"),
        uuid_col("edge_id", nullable=True),
        sa.Column("depth", sa.SmallInteger(), nullable=False),
        pk(t, "id"),
        ck_enum(t, "child_role", RecordRole),
        ck_hex(t, "parent_key_hmac"),
        ck_hex(t, "child_key_hmac"),
        ck(t, "depth_nonneg", "depth >= 0"),
        ck(t, "edge_required", "edge_id IS NOT NULL OR child_role = 'SUBJECT_ROOT'"),
        fk(t, "plan_id", "erasure_plan", ondelete="CASCADE"),
        fk(t, "edge_id", "lineage_edge", ondelete="RESTRICT"),
        schema=S,
    )
    ix(t, "plan_id", "child_table")
    ix(t, "plan_id", "parent_key_hmac")

    t = "execution_run"
    op.create_table(
        t,
        uuid_pk(),
        uuid_col("plan_id"),
        text_col("mode"),
        text_col("status"),
        sa.Column(
            "isolation_level",
            sa.Text(),
            server_default=sa.text("'SERIALIZABLE'::text"),
            nullable=False,
        ),
        ts("started_at"),
        ts("finished_at", nullable=True, now=False),
        jsonb("error", nullable=True),
        pk(t, "id"),
        ck_enum(t, "mode", ExecutionMode),
        ck_enum(t, "status", ExecutionStatus),
        ck(t, "dry_run_never_commits", "mode <> 'DRY_RUN' OR status <> 'COMMITTED'"),
        fk(t, "plan_id", "erasure_plan", ondelete="RESTRICT"),
        schema=S,
    )
    op.create_index(
        op.f("uq_execution_run_live_committed"),
        t,
        ["plan_id"],
        unique=True,
        schema=S,
        postgresql_where=sa.text("mode = 'LIVE' AND status = 'COMMITTED'"),
    )

    t = "execution_step_result"
    op.create_table(
        t,
        uuid_pk(),
        uuid_col("run_id"),
        uuid_col("plan_item_id"),
        text_col("status"),
        sa.Column("rows_affected", sa.Integer(), nullable=True),
        sa.Column("expected_rows", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        pk(t, "id"),
        uq(t, "run_id", "plan_item_id"),
        ck_enum(t, "status", StepStatus),
        ck(t, "duration_nonneg", "duration_ms >= 0"),
        fk(t, "run_id", "execution_run", ondelete="CASCADE"),
        fk(t, "plan_item_id", "plan_item", ondelete="RESTRICT"),
        schema=S,
    )

    t = "verification_run"
    op.create_table(
        t,
        uuid_pk(),
        uuid_col("execution_run_id"),
        text_col("status"),
        ts("started_at"),
        ts("finished_at", nullable=True, now=False),
        text_col("outcome", nullable=True),
        sa.Column("required_artifacts", sa.Integer(), nullable=True),
        sa.Column("verified_artifacts", sa.Integer(), nullable=True),
        pk(t, "id"),
        ck_enum(t, "status", VerificationStatus),
        ck_enum(t, "outcome", VerificationOutcome, nullable=True),
        ck(t, "coverage_bounded", "verified_artifacts <= required_artifacts"),
        fk(t, "execution_run_id", "execution_run", ondelete="RESTRICT"),
        schema=S,
    )

    t = "verification_check"
    op.create_table(
        t,
        uuid_pk(),
        uuid_col("verification_run_id"),
        text_col("check_type"),
        text_col("schema_name", nullable=True),
        text_col("table_name", nullable=True),
        text_col("column_name", nullable=True),
        uuid_col("plan_item_id", nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=False),
        jsonb("observed"),
        jsonb("expected"),
        pk(t, "id"),
        ck_enum(t, "check_type", CheckType),
        fk(t, "verification_run_id", "verification_run", ondelete="CASCADE"),
        fk(t, "plan_item_id", "plan_item"),
        schema=S,
    )
    ix(t, "verification_run_id", "passed")


def _audit_tables() -> None:
    t = "audit_event"
    op.create_table(
        t,
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column(
            "event_id",
            pg.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        ts("occurred_at"),
        text_col("event_type"),
        text_col("entity_type"),
        uuid_col("entity_id", nullable=True),
        text_col("actor"),
        text_col("payload_canonical"),
        hex_col("prev_hash"),
        hex_col("event_hash"),
        pk(t, "seq"),
        uq(t, "event_id"),
        uq(t, "event_hash"),
        ck_enum(t, "event_type", AuditEventType),
        ck_hex(t, "prev_hash"),
        ck_hex(t, "event_hash"),
        schema=S,
    )
    ix(t, "entity_type", "entity_id")

    t = "signing_key"
    op.create_table(
        t,
        text_col("key_id"),
        text_col("algorithm"),
        text_col("public_key_pem"),
        ts("created_at"),
        ts("retired_at", nullable=True, now=False),
        pk(t, "key_id"),
        ck(t, "algorithm_valid", "algorithm = 'Ed25519'"),
        schema=S,
    )

    t = "evidence_bundle"
    op.create_table(
        t,
        uuid_pk(),
        uuid_col("plan_id"),
        uuid_col("verification_run_id"),
        hex_col("merkle_root"),
        sa.Column("leaf_count", sa.Integer(), nullable=False),
        text_col("leaves_canonical"),
        sa.Column("chain_anchor_seq", sa.BigInteger(), nullable=False),
        hex_col("chain_anchor_hash"),
        text_col("bundle_canonical"),
        text_col("signature"),
        text_col("signing_key_id"),
        ts("issued_at"),
        pk(t, "id"),
        uq(t, "plan_id"),
        ck_hex(t, "merkle_root"),
        ck_hex(t, "chain_anchor_hash"),
        ck(t, "leaf_count_nonneg", "leaf_count >= 0"),
        fk(t, "plan_id", "erasure_plan", ondelete="RESTRICT"),
        fk(t, "verification_run_id", "verification_run"),
        fk(t, "chain_anchor_seq", "audit_event", "seq"),
        fk(t, "signing_key_id", "signing_key", "key_id"),
        schema=S,
    )

    t = "benchmark_run"
    op.create_table(
        t,
        uuid_pk(),
        text_col("scenario"),
        jsonb("params"),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        text_col("git_commit", nullable=True),
        jsonb("environment"),
        jsonb("metrics"),
        ts("created_at"),
        pk(t, "id"),
        ck(t, "scenario_format", "scenario ~ '^B[0-9]+$'"),
        schema=S,
    )


def _triggers() -> None:
    op.execute(AUDIT_APPEND_ONLY_FN)
    op.execute(
        "CREATE TRIGGER trg_audit_event_append_only BEFORE UPDATE OR DELETE ON lae.audit_event "
        "FOR EACH ROW EXECUTE FUNCTION lae.audit_event_append_only()"
    )
    op.execute(
        "CREATE TRIGGER trg_audit_event_append_only_truncate BEFORE TRUNCATE ON lae.audit_event "
        "FOR EACH STATEMENT EXECUTE FUNCTION lae.audit_event_append_only()"
    )
    op.execute(VERIFICATION_REQUIRES_COMMITTED_FN)
    op.execute(
        "CREATE TRIGGER trg_verification_run_requires_committed "
        "BEFORE INSERT OR UPDATE OF execution_run_id ON lae.verification_run "
        "FOR EACH ROW EXECUTE FUNCTION lae.verification_run_requires_committed()"
    )


def _grants() -> None:
    op.execute(ENSURE_READER_ROLE)
    op.execute("GRANT USAGE ON SCHEMA lae TO lae_reader")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA lae TO lae_reader")
    # Belt and braces: nobody but the owner may rewrite history.
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON lae.audit_event FROM PUBLIC")


# ------------------------------------------------------------------ downgrade


def downgrade() -> None:
    op.execute("REVOKE SELECT ON ALL TABLES IN SCHEMA lae FROM lae_reader")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_verification_run_requires_committed ON lae.verification_run"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_audit_event_append_only_truncate ON lae.audit_event")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_event_append_only ON lae.audit_event")
    op.execute("DROP FUNCTION IF EXISTS lae.verification_run_requires_committed()")
    op.execute("DROP FUNCTION IF EXISTS lae.audit_event_append_only()")
    for t in reversed(TABLES):
        op.drop_table(t, schema=S)
    # Extensions, the `lae` schema and the cluster-wide lae_reader role are shared with
    # db/init and other databases, so they are intentionally left in place.
