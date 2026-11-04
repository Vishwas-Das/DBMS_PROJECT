"""Configuration tables that survive snapshots (database_schema.md §3.2 + §7)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from lae.contracts.enums import (
    ArtifactKind,
    ErasureStrategy,
    PiiCategory,
    PiiSource,
    RefreshStrategy,
    RetentionAction,
)
from lae.db.models.base import Base, created_at, enum_check, uuid_pk

# Kinds that carry no SQL definition of their own (keyed copies or outside PostgreSQL).
KINDS_WITHOUT_DEFINITION = (ArtifactKind.EMBEDDING_TABLE, ArtifactKind.CACHE, ArtifactKind.EXTERNAL)


class DerivedArtifact(Base):
    __tablename__ = "derived_artifact"

    id: Mapped[UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    schema_name: Mapped[str] = mapped_column(Text, nullable=False)
    table_name: Mapped[str] = mapped_column(Text, nullable=False)
    definition_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_strategy: Mapped[str] = mapped_column(Text, nullable=False)
    source_key_mapping: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    is_auto_registered: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    is_inspectable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("schema_name", "table_name"),
        enum_check("kind", ArtifactKind),
        enum_check("refresh_strategy", RefreshStrategy),
        CheckConstraint(
            "kind <> 'MATERIALIZED_VIEW' OR refresh_strategy IN ('REFRESH_MATVIEW', 'NONE')",
            name="matview_strategy",
        ),
        CheckConstraint("kind <> 'VIEW' OR refresh_strategy = 'NONE'", name="view_strategy"),
        CheckConstraint(
            "refresh_strategy <> 'DELETE_BY_KEY' OR source_key_mapping IS NOT NULL",
            name="delete_by_key_mapping",
        ),
        CheckConstraint(
            "refresh_strategy <> 'RECOMPUTE_SQL' OR definition_sql IS NOT NULL",
            name="recompute_has_sql",
        ),
        CheckConstraint(
            "definition_sql IS NOT NULL OR kind IN ('EMBEDDING_TABLE', 'CACHE', 'EXTERNAL')",
            name="definition_required",
        ),
        CheckConstraint("kind <> 'EXTERNAL' OR is_inspectable = false", name="external_opaque"),
    )


class PiiClassification(Base):
    __tablename__ = "pii_classification"

    id: Mapped[UUID] = uuid_pk()
    schema_name: Mapped[str] = mapped_column(Text, nullable=False)
    table_name: Mapped[str] = mapped_column(Text, nullable=False)
    column_name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    erasure_strategy: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    is_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    use_for_residual_scan: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    __table_args__ = (
        UniqueConstraint("schema_name", "table_name", "column_name"),
        enum_check("category", PiiCategory),
        enum_check("erasure_strategy", ErasureStrategy),
        enum_check("source", PiiSource),
    )


class SubjectRootModel(Base):
    __tablename__ = "subject_root"

    id: Mapped[UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    schema_name: Mapped[str] = mapped_column(Text, nullable=False)
    table_name: Mapped[str] = mapped_column(Text, nullable=False)
    key_columns: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    identifier_columns: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )

    __table_args__ = (CheckConstraint("cardinality(key_columns) >= 1", name="key_nonempty"),)


class RetentionPolicyModel(Base):
    __tablename__ = "retention_policy"

    id: Mapped[UUID] = uuid_pk()
    schema_name: Mapped[str] = mapped_column(Text, nullable=False)
    table_name: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    legal_basis: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("schema_name", "table_name"),
        enum_check("action", RetentionAction),
        CheckConstraint(
            "action = 'DELETE' OR legal_basis IS NOT NULL", name="legal_basis_required"
        ),
    )
