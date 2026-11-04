"""Declarative base, naming convention and column helpers (docs/database_schema.md §1, §5)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import CheckConstraint, MetaData, text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

SCHEMA = "lae"
HEX64 = "~ '^[0-9a-f]{64}$'"


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA, naming_convention=NAMING_CONVENTION)


def uuid_pk() -> Mapped[UUID]:
    return mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


def created_at() -> Mapped[datetime]:
    return mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))


def tstz(*, nullable: bool = True) -> Mapped[datetime | None]:
    return mapped_column(TIMESTAMP(timezone=True), nullable=nullable)


def in_list(values: Iterable[str]) -> str:
    """SQL IN-list of fixed literals. Only ever fed with enum values from lae.contracts.enums."""
    vals = list(values)
    for v in vals:
        if not v.replace("_", "").isalnum():
            raise ValueError(f"enum literal {v!r} is not UPPER_SNAKE / alphanumeric")
    return ", ".join(f"'{v}'" for v in vals)


def enum_values(enum_cls: type[StrEnum] | Iterable[StrEnum]) -> list[str]:
    return [e.value for e in enum_cls]


def enum_check(
    col: str, enum_cls: type[StrEnum] | Iterable[StrEnum], *, nullable: bool = False
) -> CheckConstraint:
    """CHECK (col IN (...)) named `<col>_valid` (rendered as ck_<table>_<col>_valid)."""
    expr = f"{col} IN ({in_list(enum_values(enum_cls))})"
    if nullable:
        expr = f"{col} IS NULL OR {expr}"
    return CheckConstraint(expr, name=f"{col}_valid")


def hex_check(col: str, *, nullable: bool = False) -> CheckConstraint:
    expr = f"{col} {HEX64}"
    if nullable:
        expr = f"{col} IS NULL OR {expr}"
    return CheckConstraint(expr, name=f"{col}_hex")
