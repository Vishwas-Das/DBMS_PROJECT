"""Ports: the only way domain modules touch a database (docs/module_contracts.md §3).

Implemented by lae.db.target (PostgreSQL) and backend/tests/fakes (in-memory).
Every implementation MUST validate identifiers against the bound CatalogSnapshot and
raise UnknownTableError / UnknownColumnError; values are always bound parameters.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from lae.contracts.enums import AuditEventType, Normalization, OnDeleteAction
from lae.contracts.types import (
    AuditEventRecord,
    ColumnAssignment,
    ColumnRef,
    KeyTuple,
    KeyValue,
    TableInfo,
    TableRef,
)


@dataclass(frozen=True, slots=True)
class ForeignKeyRow:
    constraint_name: str
    child: TableRef
    parent: TableRef
    child_columns: tuple[str, ...]
    parent_columns: tuple[str, ...]
    on_delete: OnDeleteAction
    on_update: OnDeleteAction
    deferrable: bool


@runtime_checkable
class CatalogReader(Protocol):
    def list_tables(self, schemas: Sequence[str]) -> list[TableInfo]: ...

    def list_foreign_keys(self, schemas: Sequence[str]) -> list[ForeignKeyRow]: ...

    def sample_values(self, col: ColumnRef, limit: int) -> list[KeyValue]: ...

    def count_contained(
        self, child: ColumnRef, parent: ColumnRef, sample: Sequence[KeyValue]
    ) -> int: ...


@runtime_checkable
class TargetReader(Protocol):
    """Read-only access to target schemas."""

    def select_by_columns(
        self,
        table: TableRef,
        columns: Sequence[str],
        values: Sequence[KeyTuple],
        select: Sequence[str],
    ) -> list[dict[str, Any]]:
        """Rows of `table` WHERE (columns) IN (values), projecting `select`. Deterministic order
        (by primary key, else by `select`). Empty `values` returns []."""
        ...

    def count_by_columns(
        self, table: TableRef, columns: Sequence[str], values: Sequence[KeyTuple]
    ) -> int: ...

    def fetch_columns(
        self, table: TableRef, key_columns: Sequence[str], key: KeyTuple, columns: Sequence[str]
    ) -> dict[str, Any] | None: ...

    def run_readonly_sql(self, sql: str, params: Mapping[str, Any]) -> list[tuple[Any, ...]]: ...

    def hmac_match_count(
        self,
        col: ColumnRef,
        digests: Sequence[str],
        normalization: Normalization,
        hmac_key: bytes,
    ) -> int: ...

    def symmetric_difference_count(self, stored: TableRef, definition_sql: str) -> int: ...


@runtime_checkable
class TargetWriter(TargetReader, Protocol):
    """Only constructed inside an executor transaction (role lae_executor). Never commits."""

    def lock_rows(
        self, table: TableRef, key_columns: Sequence[str], keys: Sequence[KeyTuple]
    ) -> int: ...

    def delete_rows(
        self, table: TableRef, key_columns: Sequence[str], keys: Sequence[KeyTuple]
    ) -> int: ...

    def update_columns(
        self,
        table: TableRef,
        key_columns: Sequence[str],
        keys: Sequence[KeyTuple],
        assignments: Mapping[str, ColumnAssignment],
    ) -> int: ...

    def refresh_matview(self, table: TableRef) -> None: ...

    def recompute_table(self, table: TableRef, definition_sql: str) -> int: ...


@runtime_checkable
class AuditSink(Protocol):
    """Appends an audit event within the caller's current transaction."""

    def append(
        self,
        event_type: AuditEventType,
        entity_type: str,
        entity_id: UUID | None,
        actor: str,
        payload: Mapping[str, Any],
    ) -> AuditEventRecord: ...


RediscoverFn = Callable[[TargetReader], tuple[str, str]]
"""Returns (schema_fingerprint, record_set_hash) as seen through the given reader."""
