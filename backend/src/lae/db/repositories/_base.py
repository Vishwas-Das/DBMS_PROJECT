"""Shared repository plumbing."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, TypeVar

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lae.contracts.types import KeyTuple, TableRef
from lae.db.models import Base
from lae.errors import NotFoundError, ValidationError

M = TypeVar("M", bound=Base)


def utc_now() -> datetime:
    return datetime.now(UTC)


def key_to_json(key: Sequence[Any]) -> list[Any]:
    out: list[Any] = []
    for v in key:
        if isinstance(v, bool) or not isinstance(v, int | str):
            raise ValidationError("key values must be int or str")
        out.append(v)
    return out


def key_from_json(value: Any) -> KeyTuple:
    return tuple(value)


def keys_from_json(value: Any) -> tuple[KeyTuple, ...]:
    if value is None:
        return ()
    return tuple(tuple(k) for k in value)


def confidence_to_db(value: float) -> Decimal:
    return Decimal(repr(float(value))).quantize(Decimal("0.001"))


def table_ref(schema: str, name: str) -> TableRef:
    return TableRef(schema, name)


def constraint_of(exc: IntegrityError) -> str | None:
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None) if diag is not None else None


class Repo:
    """Base: holds the caller's Session. Repositories never commit."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _get(self, model: type[M], key: Any) -> M:
        obj = self.session.get(model, key)
        if obj is None:
            name = getattr(model, "__tablename__", model.__name__)
            raise NotFoundError(f"{name} not found", details={"entity": name, "id": str(key)})
        return obj
