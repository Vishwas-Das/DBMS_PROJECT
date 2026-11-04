"""Audit chain, signing keys and evidence bundles (database_schema.md §3.4 + §7)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from lae.contracts.enums import AuditEventType
from lae.db.models.base import Base, created_at, enum_check, hex_check, uuid_pk


class AuditEvent(Base):
    """Append-only. A trigger rejects UPDATE / DELETE / TRUNCATE (migration 0001_baseline)."""

    __tablename__ = "audit_event"

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
        unique=True,
        server_default=text("gen_random_uuid()"),
    )
    occurred_at: Mapped[datetime] = created_at()
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    payload_canonical: Mapped[str] = mapped_column(Text, nullable=False)
    prev_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False, unique=True)

    __table_args__ = (
        enum_check("event_type", AuditEventType),
        hex_check("prev_hash"),
        hex_check("event_hash"),
        Index(None, "entity_type", "entity_id"),
        # migration 0003 (QA D1): one successor per event, so the chain can never fork
        Index("uq_audit_event_prev_hash", "prev_hash", unique=True),
    )


class SigningKey(Base):
    __tablename__ = "signing_key"

    key_id: Mapped[str] = mapped_column(Text, primary_key=True)
    algorithm: Mapped[str] = mapped_column(Text, nullable=False)
    public_key_pem: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at()
    retired_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (CheckConstraint("algorithm = 'Ed25519'", name="algorithm_valid"),)


class EvidenceBundle(Base):
    __tablename__ = "evidence_bundle"

    id: Mapped[UUID] = uuid_pk()
    plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("lae.erasure_plan.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    verification_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("lae.verification_run.id"), nullable=False
    )
    merkle_root: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    leaf_count: Mapped[int] = mapped_column(Integer, nullable=False)
    leaves_canonical: Mapped[str] = mapped_column(Text, nullable=False)
    chain_anchor_seq: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("lae.audit_event.seq"), nullable=False
    )
    chain_anchor_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    bundle_canonical: Mapped[str] = mapped_column(Text, nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    signing_key_id: Mapped[str] = mapped_column(
        ForeignKey("lae.signing_key.key_id"), nullable=False
    )
    issued_at: Mapped[datetime] = created_at()

    __table_args__ = (
        hex_check("merkle_root"),
        hex_check("chain_anchor_hash"),
        CheckConstraint("leaf_count >= 0", name="leaf_count_nonneg"),
    )
