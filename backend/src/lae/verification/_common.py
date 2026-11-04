"""Internal helpers shared by the verification checks.

Nothing here logs, returns or raises raw key values: errors carry only check types, table
names and exception class names.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from lae.contracts.enums import ActionCategory, CheckType, LineageLevel
from lae.contracts.types import KeyTuple, PlanItem, TableRef, key_sort_key
from lae.errors import VerificationError

_HEX = frozenset("0123456789abcdef")


def guarded[T](check_type: CheckType, table: TableRef | None, fn: Callable[[], T]) -> T:
    """Run ``fn``; turn any failure into ``VerificationError`` (the check could not run).

    The original exception is deliberately *not* chained: database error messages can quote
    row values (for example PostgreSQL ``DETAIL: Key (email)=(...)``). Only its class name is
    kept.
    """
    try:
        return fn()
    except VerificationError:
        raise
    except Exception as exc:
        raise VerificationError(
            f"{check_type.value} check could not run",
            code="CHECK_NOT_RUNNABLE",
            details={
                "check_type": check_type.value,
                "table": table.qualified() if table is not None else None,
                "cause": type(exc).__name__,
            },
        ) from None


def as_count(value: object, check_type: CheckType, table: TableRef | None) -> int:
    """Validate that a reader returned a non-negative int count."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise VerificationError(
            f"{check_type.value}: reader returned an invalid count",
            code="INVALID_READER_RESULT",
            details={
                "check_type": check_type.value,
                "table": table.qualified() if table is not None else None,
            },
        )
    return value


def is_hex64(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in _HEX for c in value)


def require_record_level(item: PlanItem, check_type: CheckType) -> None:
    """Key-based checks need RECORD-level items (TABLE-level items carry no keys)."""
    if item.lineage_level is not LineageLevel.RECORD or item.expected_row_count is None:
        raise VerificationError(
            f"{check_type.value} needs a RECORD-level plan item",
            code="UNCHECKABLE_ITEM",
            details={
                "check_type": check_type.value,
                "table": item.table.qualified(),
                "step_order": item.step_order,
                "action": item.action.value,
            },
        )


def sorted_unique_keys(keys: Iterable[KeyTuple]) -> tuple[KeyTuple, ...]:
    return tuple(sorted(set(keys), key=key_sort_key))


def project_keys(item: PlanItem, columns: Sequence[str]) -> tuple[KeyTuple, ...] | None:
    """Project the item's record keys onto ``columns`` (None if a column is not a key column)."""
    try:
        idx = [item.key_columns.index(c) for c in columns]
    except ValueError:
        return None
    return tuple(tuple(k[i] for i in idx) for k in item.record_keys)


def deleted_keys(
    items: Iterable[PlanItem], table: TableRef, columns: Sequence[str]
) -> set[KeyTuple]:
    """Keys (projected onto ``columns``) of rows removed from ``table`` by DELETE items."""
    out: set[KeyTuple] = set()
    for it in items:
        if it.table != table or it.category is not ActionCategory.DELETE:
            continue
        projected = project_keys(it, columns)
        if projected is not None:
            out.update(projected)
    return out
