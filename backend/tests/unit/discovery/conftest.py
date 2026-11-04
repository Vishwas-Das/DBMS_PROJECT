"""Fakes and a hand-built demo dataset for lae.discovery unit tests (no database).

The fakes validate every identifier against the catalog exactly like the real adapters must
(UnknownTableError / UnknownColumnError) and record every call so tests can assert batching.
Conftest modules are not importable by tests (``--import-mode=importlib``), so everything is
handed out through the ``kit`` and ``demo`` fixtures.

Demo subject: customer 42.
- customer 77 was referred by 42 (REFERENCING_OTHER_SUBJECT); customer 5 referred 42 (upstream).
- orders 1001, 1002 (42) with lines; order 2001 belongs to 77.
- ticket 10 (42), ticket 20 (77). Messages: 100 (t10, by 42) OWNED, 101 (t10, by 77) SHARED,
  102 (t20, by 42) SHARED, 103 (t10, no author) OWNED, 104 (t20, by 77) unreachable.
- reactions: 1000 on 101 (SHARED, descendant), 1001 on 100 (OWNED), 1002 on 102 (SHARED).
- consent_log has no PK (predicate keys). marketing_event_log has no PK and no FK.
- matviews customer_ltv (KEY_PRESERVING) and daily_revenue (AGGREGATE); derived table
  customer_segment (from customer_ltv); document_embedding (registered mapping);
  customer_profile_cache (CACHE, registered mapping).
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid5

import pytest

from lae.contracts.enums import (
    ArtifactKind,
    Normalization,
    OnDeleteAction,
    RefreshStrategy,
    TableKind,
)
from lae.contracts.ports import ForeignKeyRow
from lae.contracts.types import (
    ArtifactDefinition,
    CatalogSnapshot,
    ColumnInfo,
    ColumnRef,
    KeyMapping,
    KeyTuple,
    KeyValue,
    RelationshipEdge,
    SubjectRoot,
    TableInfo,
    TableRef,
)
from lae.discovery import (
    build_catalog,
    extract_artifacts_lineage,
    foreign_key_edges,
)
from lae.errors import UnknownColumnError, UnknownTableError
from lae.lineage import LineageGraph, build_lineage_graph

NS = UUID("00000000-0000-0000-0000-0000000d15c0")


def uid(name: str) -> UUID:
    return uuid5(NS, name)


def t(name: str, schema: str = "app") -> TableRef:
    return TableRef(schema, name)


def table(
    name: str,
    columns: Sequence[tuple[str, str] | tuple[str, str, bool]],
    pk: tuple[str, ...] = (),
    kind: TableKind = TableKind.TABLE,
    definition: str | None = None,
    schema: str = "app",
    estimated_rows: int | None = None,
) -> TableInfo:
    cols = []
    for i, spec in enumerate(columns):
        nullable = spec[2] if len(spec) == 3 else spec[0] not in pk  # type: ignore[misc]
        cols.append(ColumnInfo(spec[0], i + 1, spec[1], bool(nullable)))
    return TableInfo(
        ref=t(name, schema),
        kind=kind,
        columns=tuple(cols),
        primary_key=pk,
        estimated_rows=estimated_rows,
        definition_sql=definition,
    )


def fk(
    name: str,
    child: str,
    child_cols: tuple[str, ...],
    parent: str,
    parent_cols: tuple[str, ...],
    on_delete: OnDeleteAction = OnDeleteAction.NO_ACTION,
) -> ForeignKeyRow:
    return ForeignKeyRow(
        constraint_name=name,
        child=t(child),
        parent=t(parent),
        child_columns=child_cols,
        parent_columns=parent_cols,
        on_delete=on_delete,
        on_update=OnDeleteAction.NO_ACTION,
        deferrable=False,
    )


# ---------------------------------------------------------------------- fakes


def _normalize_value(v: Any) -> Any:
    return str(v) if isinstance(v, UUID) else v


class FakeCatalogReader:
    """In-memory CatalogReader. `data` provides rows for sampling / containment."""

    def __init__(
        self,
        tables: Sequence[TableInfo],
        fks: Sequence[ForeignKeyRow] = (),
        data: Mapping[TableRef, Sequence[Mapping[str, Any]]] | None = None,
    ) -> None:
        self.tables = list(tables)
        self.fks = list(fks)
        self.data = {k: [dict(r) for r in v] for k, v in (data or {}).items()}
        self.calls: list[tuple[str, Any]] = []

    def _info(self, ref: TableRef) -> TableInfo:
        for info in self.tables:
            if info.ref == ref:
                return info
        raise UnknownTableError("unknown table", details={"table": ref.qualified()})

    def _check(self, col: ColumnRef) -> None:
        if not self._info(col.table).has_column(col.column):
            raise UnknownColumnError("unknown column", details={"column": col.qualified()})

    def list_tables(self, schemas: Sequence[str]) -> list[TableInfo]:
        self.calls.append(("list_tables", tuple(schemas)))
        return [i for i in self.tables if i.ref.schema in schemas]

    def list_foreign_keys(self, schemas: Sequence[str]) -> list[ForeignKeyRow]:
        self.calls.append(("list_foreign_keys", tuple(schemas)))
        return [f for f in self.fks if f.child.schema in schemas]

    def sample_values(self, col: ColumnRef, limit: int) -> list[KeyValue]:
        self._check(col)
        self.calls.append(("sample_values", col))
        vals = [
            r[col.column] for r in self.data.get(col.table, []) if r.get(col.column) is not None
        ]
        return vals[:limit]

    def count_contained(
        self, child: ColumnRef, parent: ColumnRef, sample: Sequence[KeyValue]
    ) -> int:
        self._check(child)
        self._check(parent)
        self.calls.append(("count_contained", (child, parent)))
        parent_vals = {r.get(parent.column) for r in self.data.get(parent.table, [])}
        return sum(1 for v in sample if v in parent_vals)


class FakeTargetReader:
    """In-memory TargetReader bound to a catalog. Validates identifiers like the real one.

    `shuffle` returns rows in a random (seeded) order to prove callers do not rely on order.
    """

    def __init__(
        self,
        catalog: CatalogSnapshot,
        data: Mapping[TableRef, Sequence[Mapping[str, Any]]],
        *,
        shuffle_seed: int | None = None,
    ) -> None:
        self.catalog = catalog
        self.data = {k: [dict(r) for r in v] for k, v in data.items()}
        self.calls: list[dict[str, Any]] = []
        self._rng = random.Random(shuffle_seed) if shuffle_seed is not None else None

    def _validate(self, table: TableRef, columns: Sequence[str]) -> TableInfo:
        info = self.catalog.table(table)  # UnknownTableError
        for c in columns:
            info.column(c)  # UnknownColumnError
        return info

    def _rows(self, table: TableRef) -> list[dict[str, Any]]:
        return self.data.get(table, [])

    def select_by_columns(
        self,
        table: TableRef,
        columns: Sequence[str],
        values: Sequence[KeyTuple],
        select: Sequence[str],
    ) -> list[dict[str, Any]]:
        info = self._validate(table, [*columns, *select])
        if not columns:
            raise UnknownColumnError("empty column list", details={"table": table.qualified()})
        for v in values:
            if len(v) != len(columns):
                raise ValueError("value tuple length does not match columns")
        self.calls.append(
            {"op": "select", "table": table, "columns": tuple(columns), "n": len(values)}
        )
        if not values:
            return []
        wanted = {tuple(v) for v in values}
        out = []
        for row in self._rows(table):
            key = tuple(_normalize_value(row.get(c)) for c in columns)
            if any(k is None for k in key) or key not in wanted:
                continue
            out.append({c: row.get(c) for c in select})
        order = list(info.primary_key) or list(select)
        out.sort(key=lambda r: tuple((str(type(r.get(c))), r.get(c)) for c in order))
        if self._rng is not None:
            self._rng.shuffle(out)
        return out

    def count_by_columns(
        self, table: TableRef, columns: Sequence[str], values: Sequence[KeyTuple]
    ) -> int:
        return len(self.select_by_columns(table, columns, values, list(columns)))

    def fetch_columns(
        self, table: TableRef, key_columns: Sequence[str], key: KeyTuple, columns: Sequence[str]
    ) -> dict[str, Any] | None:
        rows = self.select_by_columns(table, key_columns, [key], columns)
        return rows[0] if rows else None

    def run_readonly_sql(self, sql: str, params: Mapping[str, Any]) -> list[tuple[Any, ...]]:
        raise NotImplementedError

    def hmac_match_count(
        self, col: ColumnRef, digests: Sequence[str], normalization: Normalization, hmac_key: bytes
    ) -> int:
        raise NotImplementedError

    def symmetric_difference_count(self, stored: TableRef, definition_sql: str) -> int:
        raise NotImplementedError


# ---------------------------------------------------------------------- demo schema

LTV_SQL = """
SELECT c.id AS customer_id,
       count(DISTINCT o.id) AS order_count,
       COALESCE(sum(p.amount_cents), 0)::bigint AS total_spent
FROM app.customer c
LEFT JOIN app.purchase_order o ON o.customer_id = c.id
LEFT JOIN app.payment p ON p.order_id = o.id
GROUP BY c.id
"""
DAILY_REVENUE_SQL = """
SELECT date_trunc('day', p.paid_at)::date AS day,
       sum(p.amount_cents) AS revenue_cents,
       count(*) AS payment_count
FROM app.payment p
GROUP BY 1
"""
SEGMENT_SQL = """
SELECT l.customer_id,
       CASE WHEN l.total_spent >= 10000 THEN 'gold'
            WHEN l.order_count > 0 THEN 'regular'
            ELSE 'prospect' END AS segment
FROM app.customer_ltv l
"""


def demo_tables() -> list[TableInfo]:
    return [
        table(
            "customer",
            [
                ("id", "bigint"),
                ("email", "text", False),
                ("full_name", "text"),
                ("phone", "text"),
                ("referrer_id", "bigint"),
            ],
            ("id",),
        ),
        table(
            "customer_address",
            [("id", "bigint"), ("customer_id", "bigint"), ("street", "text")],
            ("id",),
        ),
        table(
            "loyalty_account", [("customer_id", "bigint"), ("points", "integer")], ("customer_id",)
        ),
        table(
            "purchase_order",
            [
                ("id", "bigint"),
                ("customer_id", "bigint"),
                ("total_cents", "bigint"),
                ("status", "text"),
            ],
            ("id",),
        ),
        table(
            "order_line",
            [
                ("order_id", "bigint"),
                ("line_no", "integer"),
                ("product_id", "bigint"),
                ("qty", "integer"),
            ],
            ("order_id", "line_no"),
        ),
        table(
            "shipment",
            [("id", "bigint"), ("order_id", "bigint"), ("line_no", "integer"), ("carrier", "text")],
            ("id",),
        ),
        table(
            "payment",
            [
                ("id", "bigint"),
                ("order_id", "bigint"),
                ("amount_cents", "bigint"),
                ("paid_at", "timestamp with time zone"),
            ],
            ("id",),
        ),
        table("product", [("id", "bigint"), ("name", "text")], ("id",)),
        table(
            "support_ticket",
            [
                ("id", "bigint"),
                ("customer_id", "bigint"),
                ("subject", "text"),
                ("contact_email", "text"),
            ],
            ("id",),
        ),
        table(
            "ticket_message",
            [
                ("id", "bigint"),
                ("ticket_id", "bigint"),
                ("author_customer_id", "bigint"),
                ("author_email", "text"),
                ("body", "text"),
            ],
            ("id",),
        ),
        table(
            "message_reaction",
            [("id", "bigint"), ("message_id", "bigint"), ("kind", "text")],
            ("id",),
        ),
        table(
            "consent_log",
            [
                ("customer_id", "bigint", False),
                ("consent", "text"),
                ("given_at", "timestamp with time zone"),
            ],
        ),
        table("marketing_event_log", [("email", "text"), ("event", "text")]),
        table(
            "document",
            [("id", "bigint"), ("customer_id", "bigint"), ("title", "text"), ("body", "text")],
            ("id",),
        ),
        table(
            "document_embedding",
            [("id", "bigint"), ("source_document_id", "bigint"), ("embedding", "vector(384)")],
            ("id",),
        ),
        table(
            "customer_profile_cache",
            [("customer_id", "bigint"), ("payload", "jsonb")],
            ("customer_id",),
        ),
        table(
            "customer_ltv",
            [("customer_id", "bigint"), ("order_count", "bigint"), ("total_spent", "bigint")],
            (),
            TableKind.MATERIALIZED_VIEW,
            LTV_SQL,
        ),
        table(
            "daily_revenue",
            [("day", "date"), ("revenue_cents", "bigint"), ("payment_count", "bigint")],
            (),
            TableKind.MATERIALIZED_VIEW,
            DAILY_REVENUE_SQL,
        ),
        table(
            "customer_segment", [("customer_id", "bigint"), ("segment", "text")], ("customer_id",)
        ),
    ]


def demo_fks() -> list[ForeignKeyRow]:
    a, r, c, n, d = (
        OnDeleteAction.NO_ACTION,
        OnDeleteAction.RESTRICT,
        OnDeleteAction.CASCADE,
        OnDeleteAction.SET_NULL,
        OnDeleteAction.SET_DEFAULT,
    )
    return [
        fk("customer_referrer_id_fkey", "customer", ("referrer_id",), "customer", ("id",), n),
        fk(
            "customer_address_customer_id_fkey",
            "customer_address",
            ("customer_id",),
            "customer",
            ("id",),
            c,
        ),
        fk(
            "loyalty_account_customer_id_fkey",
            "loyalty_account",
            ("customer_id",),
            "customer",
            ("id",),
            a,
        ),
        fk(
            "purchase_order_customer_id_fkey",
            "purchase_order",
            ("customer_id",),
            "customer",
            ("id",),
            r,
        ),
        fk("order_line_order_id_fkey", "order_line", ("order_id",), "purchase_order", ("id",), c),
        fk("order_line_product_id_fkey", "order_line", ("product_id",), "product", ("id",), a),
        fk(
            "shipment_order_line_fkey",
            "shipment",
            ("order_id", "line_no"),
            "order_line",
            ("order_id", "line_no"),
            a,
        ),
        fk("payment_order_id_fkey", "payment", ("order_id",), "purchase_order", ("id",), a),
        fk(
            "support_ticket_customer_id_fkey",
            "support_ticket",
            ("customer_id",),
            "customer",
            ("id",),
            n,
        ),
        fk(
            "ticket_message_ticket_id_fkey",
            "ticket_message",
            ("ticket_id",),
            "support_ticket",
            ("id",),
            c,
        ),
        fk(
            "ticket_message_author_customer_id_fkey",
            "ticket_message",
            ("author_customer_id",),
            "customer",
            ("id",),
            a,
        ),
        fk(
            "message_reaction_message_id_fkey",
            "message_reaction",
            ("message_id",),
            "ticket_message",
            ("id",),
            c,
        ),
        fk("consent_log_customer_id_fkey", "consent_log", ("customer_id",), "customer", ("id",), d),
        fk("document_customer_id_fkey", "document", ("customer_id",), "customer", ("id",), c),
    ]


def demo_artifacts() -> list[ArtifactDefinition]:
    return [
        ArtifactDefinition(
            uid("customer_ltv"),
            "customer_ltv",
            ArtifactKind.MATERIALIZED_VIEW,
            t("customer_ltv"),
            LTV_SQL,
            RefreshStrategy.REFRESH_MATVIEW,
        ),
        ArtifactDefinition(
            uid("daily_revenue"),
            "daily_revenue",
            ArtifactKind.MATERIALIZED_VIEW,
            t("daily_revenue"),
            DAILY_REVENUE_SQL,
            RefreshStrategy.REFRESH_MATVIEW,
        ),
        ArtifactDefinition(
            uid("customer_segment"),
            "customer_segment",
            ArtifactKind.DERIVED_TABLE,
            t("customer_segment"),
            SEGMENT_SQL,
            RefreshStrategy.RECOMPUTE_SQL,
        ),
        ArtifactDefinition(
            uid("document_embedding"),
            "document_embedding",
            ArtifactKind.EMBEDDING_TABLE,
            t("document_embedding"),
            None,
            RefreshStrategy.DELETE_BY_KEY,
            (KeyMapping(t("document"), ("id",), ("source_document_id",)),),
        ),
        ArtifactDefinition(
            uid("customer_profile_cache"),
            "customer_profile_cache",
            ArtifactKind.CACHE,
            t("customer_profile_cache"),
            None,
            RefreshStrategy.INVALIDATE_BY_KEY,
            (KeyMapping(t("customer"), ("id",), ("customer_id",)),),
        ),
    ]


def demo_data() -> dict[TableRef, list[dict[str, Any]]]:
    return {
        t("customer"): [
            {
                "id": 5,
                "email": "five@example.org",
                "full_name": "Five",
                "phone": None,
                "referrer_id": None,
            },
            {
                "id": 42,
                "email": " Alice.Subject@Example.ORG ",
                "full_name": "Alice Subject",
                "phone": "+1 (555) 010-4242",
                "referrer_id": 5,
            },
            {
                "id": 77,
                "email": "bob@example.org",
                "full_name": "Bob Other",
                "phone": "n/a",
                "referrer_id": 42,
            },
            {
                "id": 99,
                "email": "zed@example.org",
                "full_name": "Zed",
                "phone": None,
                "referrer_id": None,
            },
        ],
        t("customer_address"): [
            {"id": 1, "customer_id": 42, "street": "1 Subject Way"},
            {"id": 2, "customer_id": 42, "street": "2 Subject Way"},
            {"id": 3, "customer_id": 77, "street": "Other St"},
        ],
        t("loyalty_account"): [{"customer_id": 42, "points": 10}, {"customer_id": 99, "points": 1}],
        t("purchase_order"): [
            {"id": 1001, "customer_id": 42, "total_cents": 5000, "status": "paid"},
            {"id": 1002, "customer_id": 42, "total_cents": 7000, "status": "open"},
            {"id": 2001, "customer_id": 77, "total_cents": 100, "status": "paid"},
            {"id": 3001, "customer_id": None, "total_cents": 1, "status": "anon"},
        ],
        t("order_line"): [
            {"order_id": 1001, "line_no": 1, "product_id": 7, "qty": 1},
            {"order_id": 1001, "line_no": 2, "product_id": 7, "qty": 2},
            {"order_id": 1002, "line_no": 1, "product_id": 8, "qty": 1},
            {"order_id": 2001, "line_no": 1, "product_id": 7, "qty": 1},
        ],
        t("shipment"): [
            {"id": 1, "order_id": 1001, "line_no": 1, "carrier": "dhl"},
            {"id": 2, "order_id": 1002, "line_no": 1, "carrier": "ups"},
            {"id": 3, "order_id": 2001, "line_no": 1, "carrier": "ups"},
            {
                "id": 4,
                "order_id": 1001,
                "line_no": 9,
                "carrier": "none",
            },  # dangling line, never matched
        ],
        t("payment"): [
            {"id": 1, "order_id": 1001, "amount_cents": 5000, "paid_at": "2026-01-01"},
            {"id": 2, "order_id": 1002, "amount_cents": 7000, "paid_at": "2026-01-02"},
            {"id": 3, "order_id": 2001, "amount_cents": 100, "paid_at": "2026-01-02"},
        ],
        t("product"): [{"id": 7, "name": "Widget"}, {"id": 8, "name": "Gadget"}],
        t("support_ticket"): [
            {
                "id": 10,
                "customer_id": 42,
                "subject": "help",
                "contact_email": "ALICE.SUBJECT@example.org",
            },
            {"id": 20, "customer_id": 77, "subject": "hi", "contact_email": "bob@example.org"},
        ],
        t("ticket_message"): [
            {
                "id": 100,
                "ticket_id": 10,
                "author_customer_id": 42,
                "author_email": "alice.alt@example.org",
                "body": "a",
            },
            {
                "id": 101,
                "ticket_id": 10,
                "author_customer_id": 77,
                "author_email": "bob@example.org",
                "body": "b",
            },
            {
                "id": 102,
                "ticket_id": 20,
                "author_customer_id": 42,
                "author_email": "alice.third@example.org",
                "body": "c",
            },
            {
                "id": 103,
                "ticket_id": 10,
                "author_customer_id": None,
                "author_email": None,
                "body": "staff",
            },
            {
                "id": 104,
                "ticket_id": 20,
                "author_customer_id": 77,
                "author_email": "bob@example.org",
                "body": "d",
            },
        ],
        t("message_reaction"): [
            {"id": 1000, "message_id": 101, "kind": "like"},
            {"id": 1001, "message_id": 100, "kind": "like"},
            {"id": 1002, "message_id": 102, "kind": "like"},
            {"id": 1003, "message_id": 104, "kind": "like"},
        ],
        t("consent_log"): [
            {"customer_id": 42, "consent": "email", "given_at": "2026-01-01"},
            {"customer_id": 42, "consent": "sms", "given_at": "2026-01-02"},
            {"customer_id": 77, "consent": "email", "given_at": "2026-01-03"},
        ],
        t("marketing_event_log"): [
            {"email": "alice.subject@example.org", "event": "open"},
            {"email": "bob@example.org", "event": "click"},
        ],
        t("document"): [
            {"id": 500, "customer_id": 42, "title": "contract", "body": "Alice"},
            {"id": 501, "customer_id": 77, "title": "contract", "body": "Bob"},
        ],
        t("document_embedding"): [
            {"id": 900, "source_document_id": 500, "embedding": "[0.1]"},
            {"id": 901, "source_document_id": 501, "embedding": "[0.2]"},
        ],
        t("customer_profile_cache"): [
            {"customer_id": 42, "payload": "{}"},
            {"customer_id": 77, "payload": "{}"},
        ],
        t("customer_ltv"): [
            {"customer_id": 5, "order_count": 0, "total_spent": 0},
            {"customer_id": 42, "order_count": 2, "total_spent": 12000},
            {"customer_id": 77, "order_count": 1, "total_spent": 100},
            {"customer_id": 99, "order_count": 0, "total_spent": 0},
        ],
        t("daily_revenue"): [
            {"day": "2026-01-01", "revenue_cents": 5000, "payment_count": 1},
            {"day": "2026-01-02", "revenue_cents": 7100, "payment_count": 2},
        ],
        t("customer_segment"): [
            {"customer_id": 42, "segment": "gold"},
            {"customer_id": 77, "segment": "regular"},
        ],
    }


@dataclass
class Demo:
    catalog: CatalogSnapshot
    fks: list[ForeignKeyRow]
    fk_edges: list[RelationshipEdge]
    artifacts: list[ArtifactDefinition]
    artifact_edges: list[RelationshipEdge]
    graph: LineageGraph
    data: dict[TableRef, list[dict[str, Any]]]
    root: SubjectRoot
    extra_edges: list[RelationshipEdge] = field(default_factory=list)

    def reader(self, **kw: Any) -> FakeTargetReader:
        return FakeTargetReader(self.catalog, self.data, **kw)

    def with_edges(self, extra: Sequence[RelationshipEdge]) -> Demo:
        graph = build_lineage_graph(self.catalog, [*self.fk_edges, *self.artifact_edges, *extra])
        return Demo(
            self.catalog,
            self.fks,
            self.fk_edges,
            self.artifacts,
            self.artifact_edges,
            graph,
            self.data,
            self.root,
            list(extra),
        )


def make_demo() -> Demo:
    tables = demo_tables()
    fks = demo_fks()
    catalog = build_catalog(tables, fks, ["app"])
    fk_edges = foreign_key_edges(fks, catalog)
    artifacts = demo_artifacts()
    results = extract_artifacts_lineage(artifacts, catalog)
    artifact_edges = [e for r in results for e in r.edges]
    graph = build_lineage_graph(catalog, [*fk_edges, *artifact_edges])
    root = SubjectRoot(uid("root:customer"), t("customer"), ("id",), ("email", "phone"))
    return Demo(catalog, fks, fk_edges, artifacts, artifact_edges, graph, demo_data(), root)


@pytest.fixture
def demo() -> Demo:
    return make_demo()


@pytest.fixture
def kit() -> SimpleNamespace:
    return SimpleNamespace(
        uid=uid,
        t=t,
        table=table,
        fk=fk,
        FakeCatalogReader=FakeCatalogReader,
        FakeTargetReader=FakeTargetReader,
        demo_tables=demo_tables,
        demo_fks=demo_fks,
        demo_data=demo_data,
        demo_artifacts=demo_artifacts,
        make_demo=make_demo,
        LTV_SQL=LTV_SQL,
        DAILY_REVENUE_SQL=DAILY_REVENUE_SQL,
        SEGMENT_SQL=SEGMENT_SQL,
    )
