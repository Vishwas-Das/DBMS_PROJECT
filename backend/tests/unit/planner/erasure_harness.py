"""In-memory test harness for the planner and executor (no PostgreSQL).

- `Schema`: builds a CatalogSnapshot, lineage edges and a LineageGraph from a compact spec.
- `FakeDB`: dict-backed TargetWriter with PostgreSQL-like FK semantics: ON DELETE CASCADE /
  SET NULL, RESTRICT / NO ACTION checked at the end of each statement, NOT NULL checks, and
  statement atomicity. Every call is logged in `ops` (for dry-run equivalence).
- `discover`: a small breadth-first record discovery over CONFIRMED edges that mimics
  architecture.md §8 (downstream only, other subjects not expanded).
- `FakeAudit`: records appended events.
- `shop()`: the demo e-commerce scenario used by most tests.

Imported by tests via sys.path (see conftest.py); it is not a pytest module.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid5

from lae.audit.canonical import canonical_hash, canonical_json
from lae.contracts.enums import (
    ArtifactKind,
    AuditEventType,
    EdgeOrigin,
    EdgeStatus,
    ErasureStrategy,
    ExecutionMode,
    LineageLevel,
    Normalization,
    OnDeleteAction,
    PiiCategory,
    RecordRole,
    RefreshStrategy,
    RetentionAction,
    TableKind,
    Traceability,
    Transform,
)
from lae.contracts.types import (
    ArtifactDefinition,
    AuditEventRecord,
    CatalogSnapshot,
    ColumnAssignment,
    ColumnInfo,
    ColumnRef,
    KeyMapping,
    KeyTuple,
    PiiColumn,
    PolicySet,
    RecordKey,
    RelationshipEdge,
    TableInfo,
    TableRef,
)
from lae.errors import TargetQueryError
from lae.lineage import LineageGraph, RecordDiscoveryResult, RecordGraph, build_lineage_graph

NS = UUID("00000000-0000-0000-0000-0000000e1a5e")


def uid(name: str) -> UUID:
    return uuid5(NS, name)


def T(name: str) -> TableRef:  # noqa: N802 - short test helper for app tables
    return TableRef("app", name)


# ---------------------------------------------------------------------- schema


@dataclass(frozen=True)
class Fk:
    name: str
    child: TableRef
    child_cols: tuple[str, ...]
    parent: TableRef
    parent_cols: tuple[str, ...]
    on_delete: OnDeleteAction


@dataclass
class Schema:
    tables: dict[TableRef, TableInfo] = field(default_factory=dict)
    fks: list[Fk] = field(default_factory=list)
    extra_edges: list[RelationshipEdge] = field(default_factory=list)
    artifacts: list[ArtifactDefinition] = field(default_factory=list)
    pii: dict[ColumnRef, PiiColumn] = field(default_factory=dict)
    retention: dict[TableRef, RetentionAction] = field(default_factory=dict)

    def table(
        self,
        name: str,
        *cols: str,
        pk: tuple[str, ...] = ("id",),
        not_null: Iterable[str] = (),
        kind: TableKind = TableKind.TABLE,
    ) -> TableRef:
        nn = set(not_null) | set(pk)
        info = TableInfo(
            ref=T(name),
            kind=kind,
            columns=tuple(ColumnInfo(c, i + 1, "text", c not in nn) for i, c in enumerate(cols)),
            primary_key=pk,
        )
        self.tables[info.ref] = info
        return info.ref

    def fk(
        self,
        name: str,
        child: str,
        child_cols: Sequence[str],
        parent: str,
        parent_cols: Sequence[str],
        on_delete: OnDeleteAction,
    ) -> Fk:
        f = Fk(name, T(child), tuple(child_cols), T(parent), tuple(parent_cols), on_delete)
        self.fks.append(f)
        return f

    def edge(
        self,
        name: str,
        source: str,
        source_cols: Sequence[str],
        target: str,
        target_cols: Sequence[str],
        *,
        origin: EdgeOrigin = EdgeOrigin.REGISTERED_MAPPING,
        traceability: Traceability = Traceability.KEY_PRESERVING,
        status: EdgeStatus = EdgeStatus.CONFIRMED,
        artifact_id: UUID | None = None,
    ) -> RelationshipEdge:
        e = RelationshipEdge(
            edge_id=uid(name),
            level=LineageLevel.TABLE,
            origin=origin,
            source=T(source),
            target=T(target),
            source_columns=tuple(source_cols),
            target_columns=tuple(target_cols),
            transform=(
                Transform.AGGREGATE
                if traceability is Traceability.AGGREGATE
                else Transform.IDENTITY
            ),
            traceability=traceability,
            status=status,
            constraint_name=name,
            artifact_id=artifact_id,
        )
        self.extra_edges.append(e)
        return e

    def artifact(
        self,
        name: str,
        kind: ArtifactKind,
        target: str,
        strategy: RefreshStrategy,
        *,
        sql: str | None = None,
        mappings: Sequence[KeyMapping] = (),
    ) -> ArtifactDefinition:
        a = ArtifactDefinition(
            artifact_id=uid("artifact:" + name),
            name=name,
            kind=kind,
            target=T(target),
            definition_sql=sql,
            refresh_strategy=strategy,
            key_mappings=tuple(mappings),
        )
        self.artifacts.append(a)
        return a

    def classify(
        self,
        table: str,
        column: str,
        strategy: ErasureStrategy = ErasureStrategy.NULLIFY,
        *,
        confirmed: bool = True,
        category: PiiCategory = PiiCategory.DIRECT_IDENTIFIER,
    ) -> None:
        ref = ColumnRef(T(table), column)
        self.pii[ref] = PiiColumn(ref, category, strategy, confirmed, True)

    def policy(self, table: str, action: RetentionAction) -> None:
        self.retention[T(table)] = action

    # ------------------------------------------------------------------ derived objects

    def fk_edges(self) -> list[RelationshipEdge]:
        return [
            RelationshipEdge(
                edge_id=uid(f.name),
                level=LineageLevel.TABLE,
                origin=EdgeOrigin.DECLARED_FK,
                source=f.parent,
                target=f.child,
                source_columns=f.parent_cols,
                target_columns=f.child_cols,
                transform=Transform.FOREIGN_KEY,
                traceability=Traceability.KEY_PRESERVING,
                status=EdgeStatus.CONFIRMED,
                on_delete=f.on_delete,
                constraint_name=f.name,
            )
            for f in self.fks
        ]

    def edges(self) -> list[RelationshipEdge]:
        return self.fk_edges() + list(self.extra_edges)

    def catalog(self) -> CatalogSnapshot:
        fp = canonical_hash(
            [
                {
                    "t": t.qualified(),
                    "k": i.kind.value,
                    "pk": list(i.primary_key),
                    "c": [[c.name, c.is_nullable] for c in i.columns],
                }
                for t, i in sorted(self.tables.items())
            ]
        )
        return CatalogSnapshot(self.tables, ("app",), fp)

    def lineage(self) -> LineageGraph:
        return build_lineage_graph(self.catalog(), self.edges(), self.pii)

    def policies(self) -> PolicySet:
        return PolicySet(retention=dict(self.retention), pii=dict(self.pii))


# ---------------------------------------------------------------------- fake database


def pseudonym(table: TableRef, key: KeyTuple, column: str) -> str:
    return "anon_" + hashlib.sha256(f"{table}|{key}|{column}".encode()).hexdigest()[:16]


class FakeDB:
    """Dict-backed TargetWriter with FK semantics. Not thread-safe; one 'transaction'."""

    def __init__(self, schema: Schema) -> None:
        self.schema = schema
        self.data: dict[TableRef, list[dict[str, Any]]] = {t: [] for t in schema.tables}
        self.builders: dict[TableRef, Callable[[FakeDB], list[dict[str, Any]]]] = {}
        self.ops: list[tuple[Any, ...]] = []
        self.fail_on: Callable[[str, TableRef], bool] | None = None
        self.recompute_counts: dict[TableRef, int] = {}

    # ------------------------------------------------------------------ test helpers

    def insert(self, table: str, **row: Any) -> None:
        info = self.schema.tables[T(table)]
        full = {c.name: row.get(c.name) for c in info.columns}
        unknown = set(row) - set(full)
        assert not unknown, unknown
        self.data[T(table)].append(full)

    def snapshot(self) -> dict[TableRef, list[dict[str, Any]]]:
        return copy.deepcopy(self.data)

    def restore(self, snap: dict[TableRef, list[dict[str, Any]]]) -> None:
        self.data = copy.deepcopy(snap)

    def checksum(self) -> str:
        def norm(rows: list[dict[str, Any]]) -> list[str]:
            return sorted(canonical_json(r).decode() for r in rows)

        return canonical_hash({t.qualified(): norm(r) for t, r in sorted(self.data.items())})

    def rows(self, table: str) -> list[dict[str, Any]]:
        return self.data[T(table)]

    def ids(self, table: str, col: str = "id") -> list[Any]:
        return sorted(r[col] for r in self.data[T(table)])

    def refresh_builders(self) -> None:
        for t, b in self.builders.items():
            self.data[t] = b(self)

    # ------------------------------------------------------------------ internals

    def _maybe_fail(self, op: str, table: TableRef) -> None:
        if self.fail_on is not None and self.fail_on(op, table):
            # deliberately contains a raw value, which must never reach the executor's error
            raise RuntimeError("injected failure for subject 42 alice@example.com")

    def _match(
        self, table: TableRef, columns: Sequence[str], values: Sequence[KeyTuple]
    ) -> list[dict[str, Any]]:
        if table not in self.data:
            raise TargetQueryError("unknown table", details={"table": table.qualified()})
        wanted = {tuple(v) for v in values}
        return [r for r in self.data[table] if tuple(r[c] for c in columns) in wanted]

    def _check_integrity(self) -> None:
        for f in self.schema.fks:
            parents = {tuple(r[c] for c in f.parent_cols) for r in self.data[f.parent]}
            for r in self.data[f.child]:
                vals = tuple(r[c] for c in f.child_cols)
                if any(v is None for v in vals):
                    continue
                if vals not in parents:
                    raise TargetQueryError(
                        f"foreign key violation on {f.name}", details={"constraint": f.name}
                    )
        for t, info in self.schema.tables.items():
            for c in info.columns:
                if not c.is_nullable and any(r[c.name] is None for r in self.data[t]):
                    raise TargetQueryError(
                        f"not-null violation on {t}.{c.name}",
                        details={"table": t.qualified(), "column": c.name},
                    )

    def _cascade_delete(self, table: TableRef, victims: list[dict[str, Any]]) -> None:
        if not victims:
            return
        ids = {id(v) for v in victims}
        self.data[table] = [r for r in self.data[table] if id(r) not in ids]
        for f in self.schema.fks:
            if f.parent != table:
                continue
            gone = {tuple(v[c] for c in f.parent_cols) for v in victims}
            still = {tuple(r[c] for c in f.parent_cols) for r in self.data[table]}
            gone -= still
            kids = [r for r in self.data[f.child] if tuple(r[c] for c in f.child_cols) in gone]
            if f.on_delete is OnDeleteAction.CASCADE:
                self._cascade_delete(f.child, kids)
            elif f.on_delete in (OnDeleteAction.SET_NULL, OnDeleteAction.SET_DEFAULT):
                for k in kids:
                    for c in f.child_cols:
                        k[c] = None

    def _statement(self, fn: Callable[[], int]) -> int:
        snap = self.snapshot()
        try:
            n = fn()
            self._check_integrity()
        except Exception:
            self.data = snap
            raise
        return n

    # ------------------------------------------------------------------ TargetReader

    def select_by_columns(
        self,
        table: TableRef,
        columns: Sequence[str],
        values: Sequence[KeyTuple],
        select: Sequence[str],
    ) -> list[dict[str, Any]]:
        self.ops.append(
            ("select", table, tuple(columns), tuple(sorted(map(tuple, values), key=repr)))
        )
        out = [{c: r[c] for c in select} for r in self._match(table, columns, values)]
        return sorted(out, key=repr)

    def count_by_columns(
        self, table: TableRef, columns: Sequence[str], values: Sequence[KeyTuple]
    ) -> int:
        self.ops.append(("count", table, tuple(columns), tuple(map(tuple, values))))
        self._maybe_fail("count", table)
        return len(self._match(table, columns, values))

    def fetch_columns(
        self, table: TableRef, key_columns: Sequence[str], key: KeyTuple, columns: Sequence[str]
    ) -> dict[str, Any] | None:
        found = self._match(table, key_columns, [key])
        return {c: found[0][c] for c in columns} if found else None

    def run_readonly_sql(self, sql: str, params: Mapping[str, Any]) -> list[tuple[Any, ...]]:
        raise NotImplementedError

    def hmac_match_count(
        self, col: ColumnRef, digests: Sequence[str], normalization: Normalization, hmac_key: bytes
    ) -> int:
        raise NotImplementedError

    def symmetric_difference_count(self, stored: TableRef, definition_sql: str) -> int:
        raise NotImplementedError

    # ------------------------------------------------------------------ TargetWriter

    def lock_rows(
        self, table: TableRef, key_columns: Sequence[str], keys: Sequence[KeyTuple]
    ) -> int:
        self.ops.append(("lock", table, tuple(key_columns), tuple(map(tuple, keys))))
        self._maybe_fail("lock", table)
        return len(self._match(table, key_columns, keys))

    def delete_rows(
        self, table: TableRef, key_columns: Sequence[str], keys: Sequence[KeyTuple]
    ) -> int:
        self.ops.append(("delete", table, tuple(key_columns), tuple(map(tuple, keys))))
        self._maybe_fail("delete", table)

        def run() -> int:
            victims = self._match(table, key_columns, keys)
            self._cascade_delete(table, victims)
            return len(victims)

        return self._statement(run)

    def update_columns(
        self,
        table: TableRef,
        key_columns: Sequence[str],
        keys: Sequence[KeyTuple],
        assignments: Mapping[str, ColumnAssignment],
    ) -> int:
        self.ops.append(
            (
                "update",
                table,
                tuple(key_columns),
                tuple(map(tuple, keys)),
                tuple(sorted((c, a.kind) for c, a in assignments.items())),
            )
        )
        self._maybe_fail("update", table)
        pk = self.schema.tables[table].primary_key

        def run() -> int:
            hit = self._match(table, key_columns, keys)
            for r in hit:
                for c, a in assignments.items():
                    key = tuple(r[k] for k in pk) if pk else tuple(r[k] for k in key_columns)
                    r[c] = None if a.kind == "NULL" else pseudonym(table, key, c)
            return len(hit)

        return self._statement(run)

    def refresh_matview(self, table: TableRef) -> None:
        self.ops.append(("refresh", table))
        self._maybe_fail("refresh", table)
        if table in self.builders:
            self.data[table] = self.builders[table](self)

    def recompute_table(self, table: TableRef, definition_sql: str) -> int:
        self.ops.append(("recompute", table, definition_sql))
        self._maybe_fail("recompute", table)
        if table in self.builders:
            self.data[table] = self.builders[table](self)
        return self.recompute_counts.get(table, len(self.data[table]))


# ---------------------------------------------------------------------- fake discovery


def _record_key(info: TableInfo, row: Mapping[str, Any], via_cols: tuple[str, ...]) -> RecordKey:
    if info.primary_key:
        return RecordKey(info.ref, info.primary_key, tuple(row[c] for c in info.primary_key))
    return RecordKey(info.ref, via_cols, tuple(row[c] for c in via_cols), is_predicate=True)


def discover(
    schema: Schema,
    reader: FakeDB,
    root: str,
    subject: KeyTuple,
    *,
    root_cols: tuple[str, ...] = ("id",),
    affected: Mapping[TableRef, Traceability] | None = None,
) -> RecordDiscoveryResult:
    """BFS over CONFIRMED TABLE edges, downstream only. Rows of other subjects stop the walk."""
    lg = schema.lineage()
    root_t = T(root)
    rg = RecordGraph()
    start = RecordKey(root_t, root_cols, subject)
    if reader.fetch_columns(root_t, root_cols, subject, root_cols) is None:
        raise AssertionError("subject not found")
    rg.add_root(start)
    marks: dict[TableRef, Traceability] = dict(affected or {})
    root_fks = [f for f in schema.fks if f.parent == root_t]
    frontier = [start]
    while frontier:
        nxt: list[RecordKey] = []
        for rec in frontier:
            if rg.attrs(rec).role in (RecordRole.REFERENCING_OTHER_SUBJECT, RecordRole.SHARED):
                continue
            if rec.is_predicate:
                continue
            for e in lg.downstream(rec.table):
                if e.traceability is not Traceability.KEY_PRESERVING or not e.source_columns:
                    marks.setdefault(e.target, e.traceability)
                    continue
                parent = reader.fetch_columns(rec.table, rec.columns, rec.values, e.source_columns)
                if parent is None:
                    continue
                vals = tuple(parent[c] for c in e.source_columns)
                if any(v is None for v in vals):
                    continue
                info = schema.tables[e.target]
                cols = [c.name for c in info.columns]
                if e.artifact_id is not None:
                    marks.setdefault(e.target, Traceability.KEY_PRESERVING)
                for row in reader.select_by_columns(e.target, e.target_columns, [vals], cols):
                    key = _record_key(info, row, e.target_columns)
                    if e.target == root_t and key != start:
                        role = RecordRole.REFERENCING_OTHER_SUBJECT
                    elif any(
                        f.child == e.target
                        and row[f.child_cols[0]] is not None
                        and (row[f.child_cols[0]],) != subject
                        for f in root_fks
                    ):
                        role = RecordRole.SHARED
                    else:
                        role = RecordRole.OWNED
                    if key == start:
                        continue
                    if rg.add_dependency(rec, key, e.edge_id, role):
                        nxt.append(key)
        frontier = nxt
    return RecordDiscoveryResult(rg, marks, ())


def rediscover_fn(
    schema: Schema, root: str, subject: KeyTuple, **kw: Any
) -> Callable[[Any], tuple[str, str]]:
    def fn(reader: Any) -> tuple[str, str]:
        res = discover(schema, reader, root, subject, **kw)
        return schema.catalog().fingerprint, res.record_graph.record_set_hash()

    return fn


# ---------------------------------------------------------------------- audit / clock


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[tuple[AuditEventType, str, UUID | None, str, Mapping[str, Any]]] = []

    def append(
        self,
        event_type: AuditEventType,
        entity_type: str,
        entity_id: UUID | None,
        actor: str,
        payload: Mapping[str, Any],
    ) -> AuditEventRecord:
        canonical = canonical_json(payload).decode()  # must canonicalize (no floats, aware dt)
        self.events.append((event_type, entity_type, entity_id, actor, payload))
        return AuditEventRecord(
            seq=len(self.events),
            event_type=event_type.value,
            entity_type=entity_type,
            entity_id=entity_id,
            actor=actor,
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
            payload_canonical=canonical,
            prev_hash="0" * 64,
            event_hash="0" * 64,
        )


class StepClock:
    """Deterministic clock: every call advances by `step_ms` milliseconds."""

    def __init__(self, step_ms: int = 5) -> None:
        self.t = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
        self.step = timedelta(milliseconds=step_ms)

    def __call__(self) -> datetime:
        self.t += self.step
        return self.t


def run_in_txn(db: FakeDB, mode: ExecutionMode, fn: Callable[[], Any]) -> Any:
    """Simulate the service-owned transaction: roll back on error and for DRY_RUN."""
    snap = db.snapshot()
    try:
        result = fn()
    except Exception:
        db.restore(snap)
        raise
    if mode is ExecutionMode.DRY_RUN:
        db.restore(snap)
    return result


# ---------------------------------------------------------------------- demo scenario


@dataclass
class Shop:
    schema: Schema
    db: FakeDB
    ltv: ArtifactDefinition
    revenue: ArtifactDefinition
    segment: ArtifactDefinition
    embedding: ArtifactDefinition
    cache: ArtifactDefinition

    def lineage(self) -> LineageGraph:
        return self.schema.lineage()

    def discover(self, subject: int = 42) -> RecordDiscoveryResult:
        return discover(self.schema, self.db, "customer", (subject,))

    def rediscover(self, subject: int = 42) -> Callable[[Any], tuple[str, str]]:
        return rediscover_fn(self.schema, "customer", (subject,))

    @property
    def artifacts(self) -> list[ArtifactDefinition]:
        return list(self.schema.artifacts)


SEGMENT_SQL = (
    "SELECT customer_id, CASE WHEN orders > 1 THEN 'gold' ELSE 'basic' END FROM app.customer_ltv"
)


def shop(*, shared_message: bool = True, with_artifacts: bool = True) -> Shop:
    s = Schema()
    s.table("customer", "id", "email", "name", "referrer_id", not_null=("email",))
    s.table("customer_address", "id", "customer_id", "street", not_null=("customer_id",))
    s.table("purchase_order", "id", "customer_id", "shipping_name")
    s.table("product", "id", "title")
    s.table(
        "order_line",
        "order_id",
        "line_no",
        "product_id",
        "qty",
        pk=("order_id", "line_no"),
        not_null=("product_id",),
    )
    s.table("shipment", "id", "order_id", "line_no", not_null=("order_id", "line_no"))
    s.table("payment", "id", "order_id", "amount", not_null=("order_id",))
    s.table("support_ticket", "id", "customer_id", "subject")
    s.table(
        "ticket_message",
        "id",
        "ticket_id",
        "author_customer_id",
        "body",
        not_null=("ticket_id", "author_customer_id"),
    )
    s.table("marketing_event_log", "customer_id", "email", pk=())
    s.fk(
        "fk_customer_referrer",
        "customer",
        ["referrer_id"],
        "customer",
        ["id"],
        OnDeleteAction.SET_NULL,
    )
    s.fk(
        "fk_address_customer",
        "customer_address",
        ["customer_id"],
        "customer",
        ["id"],
        OnDeleteAction.CASCADE,
    )
    s.fk(
        "fk_po_customer",
        "purchase_order",
        ["customer_id"],
        "customer",
        ["id"],
        OnDeleteAction.RESTRICT,
    )
    s.fk(
        "fk_ol_order", "order_line", ["order_id"], "purchase_order", ["id"], OnDeleteAction.CASCADE
    )
    s.fk("fk_ol_product", "order_line", ["product_id"], "product", ["id"], OnDeleteAction.NO_ACTION)
    s.fk(
        "fk_shipment_line",
        "shipment",
        ["order_id", "line_no"],
        "order_line",
        ["order_id", "line_no"],
        OnDeleteAction.NO_ACTION,
    )
    s.fk(
        "fk_payment_order",
        "payment",
        ["order_id"],
        "purchase_order",
        ["id"],
        OnDeleteAction.NO_ACTION,
    )
    s.fk(
        "fk_ticket_customer",
        "support_ticket",
        ["customer_id"],
        "customer",
        ["id"],
        OnDeleteAction.SET_NULL,
    )
    s.fk(
        "fk_tm_ticket",
        "ticket_message",
        ["ticket_id"],
        "support_ticket",
        ["id"],
        OnDeleteAction.CASCADE,
    )
    s.fk(
        "fk_tm_author",
        "ticket_message",
        ["author_customer_id"],
        "customer",
        ["id"],
        OnDeleteAction.NO_ACTION,
    )
    s.edge(
        "inf_marketing_customer",
        "customer",
        ["id"],
        "marketing_event_log",
        ["customer_id"],
        origin=EdgeOrigin.INFERRED,
    )
    s.classify("customer", "email")
    s.classify("customer", "name")
    s.classify("purchase_order", "shipping_name", ErasureStrategy.PSEUDONYMIZE)
    s.classify("ticket_message", "body")

    if with_artifacts:
        s.table(
            "customer_ltv",
            "customer_id",
            "orders",
            pk=("customer_id",),
            kind=TableKind.MATERIALIZED_VIEW,
        )
        s.table("daily_revenue", "day", "total", pk=("day",), kind=TableKind.MATERIALIZED_VIEW)
        s.table("customer_segment", "customer_id", "segment", pk=("customer_id",))
        s.table("document_embedding", "id", "source_ticket_id", "embedding")
        s.table("customer_profile_cache", "customer_id", "payload", pk=("customer_id",))
    ltv = s.artifact(
        "customer_ltv",
        ArtifactKind.MATERIALIZED_VIEW,
        "customer_ltv",
        RefreshStrategy.REFRESH_MATVIEW,
        sql="SELECT ...",
    )
    revenue = s.artifact(
        "daily_revenue",
        ArtifactKind.MATERIALIZED_VIEW,
        "daily_revenue",
        RefreshStrategy.REFRESH_MATVIEW,
        sql="SELECT ...",
    )
    segment = s.artifact(
        "customer_segment",
        ArtifactKind.DERIVED_TABLE,
        "customer_segment",
        RefreshStrategy.RECOMPUTE_SQL,
        sql=SEGMENT_SQL,
    )
    embedding = s.artifact(
        "document_embedding",
        ArtifactKind.EMBEDDING_TABLE,
        "document_embedding",
        RefreshStrategy.DELETE_BY_KEY,
        mappings=[KeyMapping(T("support_ticket"), ("id",), ("source_ticket_id",))],
    )
    cache = s.artifact(
        "customer_profile_cache",
        ArtifactKind.CACHE,
        "customer_profile_cache",
        RefreshStrategy.INVALIDATE_BY_KEY,
        mappings=[KeyMapping(T("customer"), ("id",), ("customer_id",))],
    )
    if not with_artifacts:
        s.artifacts.clear()
    else:
        s.edge(
            "map_ltv",
            "customer",
            ["id"],
            "customer_ltv",
            ["customer_id"],
            origin=EdgeOrigin.SQL_LINEAGE,
            artifact_id=ltv.artifact_id,
        )
        s.edge(
            "agg_po_ltv",
            "purchase_order",
            [],
            "customer_ltv",
            [],
            origin=EdgeOrigin.SQL_LINEAGE,
            traceability=Traceability.AGGREGATE,
            artifact_id=ltv.artifact_id,
        )
        s.edge(
            "agg_revenue",
            "payment",
            [],
            "daily_revenue",
            [],
            origin=EdgeOrigin.SQL_LINEAGE,
            traceability=Traceability.AGGREGATE,
            artifact_id=revenue.artifact_id,
        )
        s.edge(
            "map_segment",
            "customer_ltv",
            ["customer_id"],
            "customer_segment",
            ["customer_id"],
            origin=EdgeOrigin.SQL_LINEAGE,
            artifact_id=segment.artifact_id,
        )
        s.edge(
            "map_embedding",
            "support_ticket",
            ["id"],
            "document_embedding",
            ["source_ticket_id"],
            artifact_id=embedding.artifact_id,
        )
        s.edge(
            "map_cache",
            "customer",
            ["id"],
            "customer_profile_cache",
            ["customer_id"],
            artifact_id=cache.artifact_id,
        )

    db = FakeDB(s)
    db.insert("customer", id=42, email="alice@example.com", name="Alice", referrer_id=None)
    db.insert("customer", id=77, email="bob@example.com", name="Bob", referrer_id=42)
    db.insert("customer", id=99, email="carol@example.com", name="Carol", referrer_id=None)
    for i, c in ((1, 42), (2, 42), (3, 99)):
        db.insert("customer_address", id=i, customer_id=c, street=f"street {i}")
    for o, c in ((1001, 42), (1002, 42), (2001, 99)):
        db.insert("purchase_order", id=o, customer_id=c, shipping_name=f"name {c}")
    db.insert("product", id=7, title="widget")
    for o, n in ((1001, 1), (1001, 2), (1002, 1), (2001, 1)):
        db.insert("order_line", order_id=o, line_no=n, product_id=7, qty=1)
    db.insert("shipment", id=501, order_id=1001, line_no=1)
    db.insert("shipment", id=502, order_id=2001, line_no=1)
    db.insert("payment", id=601, order_id=1001, amount=10)
    db.insert("payment", id=602, order_id=2001, amount=20)
    db.insert("support_ticket", id=301, customer_id=42, subject="help")
    db.insert("support_ticket", id=302, customer_id=99, subject="other")
    db.insert("ticket_message", id=401, ticket_id=301, author_customer_id=42, body="hi")
    if shared_message:
        db.insert("ticket_message", id=402, ticket_id=301, author_customer_id=77, body="reply")
    db.insert("ticket_message", id=403, ticket_id=302, author_customer_id=99, body="x")
    db.insert("marketing_event_log", customer_id=42, email="alice@example.com")
    db.insert("marketing_event_log", customer_id=42, email="alice@example.com")
    db.insert("marketing_event_log", customer_id=99, email="carol@example.com")
    if with_artifacts:
        db.insert("document_embedding", id=801, source_ticket_id=301, embedding="v1")
        db.insert("document_embedding", id=802, source_ticket_id=302, embedding="v2")
        db.insert("customer_profile_cache", customer_id=42, payload="{}")
        db.insert("customer_profile_cache", customer_id=99, payload="{}")

        def build_ltv(d: FakeDB) -> list[dict[str, Any]]:
            out = []
            for c in d.rows("customer"):
                n = sum(1 for o in d.rows("purchase_order") if o["customer_id"] == c["id"])
                out.append({"customer_id": c["id"], "orders": n})
            return out

        def build_revenue(d: FakeDB) -> list[dict[str, Any]]:
            return [{"day": "2026-01-01", "total": sum(p["amount"] for p in d.rows("payment"))}]

        def build_segment(d: FakeDB) -> list[dict[str, Any]]:
            return [
                {"customer_id": r["customer_id"], "segment": "gold" if r["orders"] > 1 else "basic"}
                for r in d.rows("customer_ltv")
            ]

        db.builders[T("customer_ltv")] = build_ltv
        db.builders[T("daily_revenue")] = build_revenue
        db.builders[T("customer_segment")] = build_segment
        db.refresh_builders()
    return Shop(s, db, ltv, revenue, segment, embedding, cache)


# ---------------------------------------------------------------------- planning shortcuts

REQUEST_ID = uid("request")
PLAN_ID = uid("plan")


def make_plan(
    schema: Schema,
    db: FakeDB,
    root: str = "customer",
    subject: KeyTuple = (42,),
    *,
    artifacts: Sequence[ArtifactDefinition] | None = None,
    external: Sequence[ArtifactDefinition] = (),
    predicate_row_counts: Mapping[RecordKey, int] | None = None,
    affected: Mapping[TableRef, Traceability] | None = None,
) -> Any:
    from lae.planner import build_plan

    disc = discover(schema, db, root, subject, affected=affected)
    return build_plan(
        REQUEST_ID,
        disc,
        schema.lineage(),
        schema.policies(),
        schema.artifacts if artifacts is None else artifacts,
        external,
        plan_id=PLAN_ID,
        predicate_row_counts=predicate_row_counts,
    )


def ack_all(plan: Any) -> Any:
    """Acknowledge every acknowledgeable, unresolved conflict."""
    from lae.planner import ACKNOWLEDGEABLE_CODES, acknowledge_conflict

    for i, c in enumerate(plan.conflicts):
        if not c.resolved and c.code in ACKNOWLEDGEABLE_CODES:
            plan = acknowledge_conflict(plan, i)
    return plan


def items_of(plan: Any, action: Any, table: str | None = None) -> list[Any]:
    return [i for i in plan.items if i.action is action and (table is None or i.table == T(table))]


def codes(plan: Any) -> set[Any]:
    return {c.code for c in plan.conflicts}


def marketing_counts(db: FakeDB, subject: int = 42) -> dict[RecordKey, int]:
    key = RecordKey(T("marketing_event_log"), ("customer_id",), (subject,), is_predicate=True)
    n = sum(1 for r in db.rows("marketing_event_log") if r["customer_id"] == subject)
    return {key: n}
