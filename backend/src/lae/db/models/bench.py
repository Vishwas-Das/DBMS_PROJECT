"""Benchmark results (database_schema.md §3.4)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from lae.db.models.base import Base, created_at, uuid_pk


class BenchmarkRun(Base):
    __tablename__ = "benchmark_run"

    id: Mapped[UUID] = uuid_pk()
    scenario: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    git_commit: Mapped[str | None] = mapped_column(Text, nullable=True)
    environment: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (CheckConstraint("scenario ~ '^B[0-9]+$'", name="scenario_format"),)
