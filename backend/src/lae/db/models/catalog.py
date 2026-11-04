"""Catalog snapshot and lineage tables (database_schema.md §3.1)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lae.contracts.enums import (
    EdgeOrigin,
    EdgeStatus,
    LineageLevel,
    OnDeleteAction,
    SnapshotStatus,
    TableKind,
    Traceability,
    Transform,
)
from lae.db.models.base import Base, created_at, enum_check, hex_check, uuid_pk

EDGE_LEVELS = (LineageLevel.TABLE, LineageLevel.COLUMN)


class LineageSnapshot(Base):
    __tablename__ = "lineage_snapshot"

    id: Mapped[UUID] = uuid_pk()
    created_at: Mapped[datetime] = created_at()
    scanned_schemas: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    schema_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    table_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    edge_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    warnings: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    tables: Mapped[list[CatalogTable]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        enum_check("status", SnapshotStatus),
        hex_check("schema_fingerprint"),
        CheckConstraint("cardinality(scanned_schemas) > 0", name="schemas_nonempty"),
        CheckConstraint("table_count >= 0", name="table_count_nonneg"),
        CheckConstraint("edge_count >= 0", name="edge_count_nonneg"),
    )


Index("ix_lineage_snapshot_created_at", LineageSnapshot.created_at.desc())


class CatalogTable(Base):
    __tablename__ = "catalog_table"

    id: Mapped[UUID] = uuid_pk()
    snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("lae.lineage_snapshot.id", ondelete="CASCADE"), nullable=False
    )
    schema_name: Mapped[str] = mapped_column(Text, nullable=False)
    table_name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    primary_key_columns: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    estimated_rows: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    definition_sql: Mapped[str | None] = mapped_column(Text, nullable=True)

    snapshot: Mapped[LineageSnapshot] = relationship(back_populates="tables")
    columns: Mapped[list[CatalogColumn]] = relationship(
        back_populates="table",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="CatalogColumn.ordinal_position",
    )

    __table_args__ = (
        UniqueConstraint("snapshot_id", "schema_name", "table_name"),
        enum_check("kind", TableKind),
    )


class CatalogColumn(Base):
    __tablename__ = "catalog_column"

    id: Mapped[UUID] = uuid_pk()
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("lae.catalog_table.id", ondelete="CASCADE"), nullable=False
    )
    column_name: Mapped[str] = mapped_column(Text, nullable=False)
    ordinal_position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    data_type: Mapped[str] = mapped_column(Text, nullable=False)
    is_nullable: Mapped[bool] = mapped_column(Boolean, nullable=False)

    table: Mapped[CatalogTable] = relationship(back_populates="columns")

    __table_args__ = (
        UniqueConstraint("table_id", "column_name"),
        UniqueConstraint("table_id", "ordinal_position"),
        CheckConstraint("ordinal_position > 0", name="ordinal_positive"),
    )


class LineageEdge(Base):
    __tablename__ = "lineage_edge"

    id: Mapped[UUID] = uuid_pk()
    snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("lae.lineage_snapshot.id", ondelete="CASCADE"), nullable=False
    )
    level: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    source_table_id: Mapped[UUID] = mapped_column(
        ForeignKey("lae.catalog_table.id", ondelete="CASCADE"), nullable=False
    )
    target_table_id: Mapped[UUID] = mapped_column(
        ForeignKey("lae.catalog_table.id", ondelete="CASCADE"), nullable=False
    )
    source_columns: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    target_columns: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    transform: Mapped[str] = mapped_column(Text, nullable=False)
    traceability: Mapped[str] = mapped_column(Text, nullable=False)
    on_delete_action: Mapped[str | None] = mapped_column(CHAR(1), nullable=True)
    constraint_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, server_default=text("1")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("lae.derived_artifact.id", ondelete="SET NULL"), nullable=True
    )
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        enum_check("level", EDGE_LEVELS),
        enum_check("origin", EdgeOrigin),
        enum_check("transform", Transform),
        enum_check("traceability", Traceability),
        enum_check("status", EdgeStatus),
        enum_check("on_delete_action", OnDeleteAction, nullable=True),
        CheckConstraint(
            "cardinality(source_columns) = cardinality(target_columns)", name="col_arity"
        ),
        CheckConstraint(
            "level <> 'COLUMN' OR (cardinality(source_columns) >= 1 "
            "AND cardinality(target_columns) >= 1)",
            name="column_has_cols",
        ),
        CheckConstraint(
            "origin <> 'DECLARED_FK' OR (on_delete_action IS NOT NULL AND status = 'CONFIRMED')",
            name="fk_confirmed",
        ),
        CheckConstraint(
            "origin <> 'INFERRED' OR confidence < 1 OR status <> 'SUGGESTED'",
            name="inferred_confidence",
        ),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence_range"),
        Index(None, "snapshot_id", "source_table_id", "level"),
        Index(None, "snapshot_id", "target_table_id", "level"),
        Index(None, "snapshot_id", "status"),
    )
