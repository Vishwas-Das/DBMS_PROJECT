"""Small e-commerce catalog for lineage tests.

customer(id) ─fk_po_customer→ purchase_order(id, customer_id) ─fk_ol_order→ order_line
(order_id, line_no) ←fk_ol_product─ product(id); customer.referrer_id → customer.id (self FK);
customer / purchase_order → customer_ltv (matview) → customer_segment (matview);
customer → newsletter is an INFERRED, SUGGESTED edge (must be excluded by default).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid5

import pytest

from lae.contracts.enums import (
    EdgeOrigin,
    EdgeStatus,
    LineageLevel,
    OnDeleteAction,
    TableKind,
    Traceability,
    Transform,
)
from lae.contracts.types import (
    CatalogSnapshot,
    ColumnInfo,
    RelationshipEdge,
    TableInfo,
    TableRef,
)

NS = UUID("00000000-0000-0000-0000-00000000a1a1")


def uid(name: str) -> UUID:
    return uuid5(NS, name)


def t(name: str) -> TableRef:
    return TableRef("app", name)


def _table(
    name: str,
    cols: list[tuple[str, str, bool]],
    pk: tuple[str, ...],
    kind: TableKind = TableKind.TABLE,
) -> TableInfo:
    return TableInfo(
        ref=t(name),
        kind=kind,
        columns=tuple(ColumnInfo(c, i + 1, dt, nn) for i, (c, dt, nn) in enumerate(cols)),
        primary_key=pk,
    )


def make_catalog() -> CatalogSnapshot:
    tables = [
        _table(
            "customer",
            [("id", "integer", False), ("email", "text", False), ("referrer_id", "integer", True)],
            ("id",),
        ),
        _table(
            "purchase_order",
            [("id", "integer", False), ("customer_id", "integer", False)],
            ("id",),
        ),
        _table(
            "order_line",
            [
                ("order_id", "integer", False),
                ("line_no", "integer", False),
                ("product_id", "integer", False),
            ],
            ("order_id", "line_no"),
        ),
        _table("product", [("id", "integer", False), ("name", "text", False)], ("id",)),
        _table(
            "newsletter", [("id", "integer", False), ("subscriber_ref", "integer", True)], ("id",)
        ),
        _table(
            "customer_ltv",
            [("customer_id", "integer", False), ("total_spent", "numeric", True)],
            (),
            TableKind.MATERIALIZED_VIEW,
        ),
        _table(
            "customer_segment",
            [("customer_id", "integer", False), ("segment", "text", True)],
            (),
            TableKind.MATERIALIZED_VIEW,
        ),
    ]
    return CatalogSnapshot(
        tables={x.ref: x for x in tables}, schemas=("app",), fingerprint="f" * 64
    )


def edge(
    name: str,
    level: LineageLevel,
    src: str,
    tgt: str,
    src_cols: tuple[str, ...],
    tgt_cols: tuple[str, ...],
    *,
    origin: EdgeOrigin = EdgeOrigin.DECLARED_FK,
    transform: Transform = Transform.FOREIGN_KEY,
    traceability: Traceability = Traceability.KEY_PRESERVING,
    status: EdgeStatus = EdgeStatus.CONFIRMED,
    on_delete: OnDeleteAction | None = OnDeleteAction.NO_ACTION,
    constraint: str | None = None,
    artifact_id: UUID | None = None,
    confidence: float = 1.0,
) -> RelationshipEdge:
    return RelationshipEdge(
        edge_id=uid(name),
        level=level,
        origin=origin,
        source=t(src),
        target=t(tgt),
        source_columns=src_cols,
        target_columns=tgt_cols,
        transform=transform,
        traceability=traceability,
        status=status,
        on_delete=on_delete,
        constraint_name=constraint,
        confidence=confidence,
        artifact_id=artifact_id,
    )


def fk_pair(
    name: str, parent: str, child: str, pcols: tuple[str, ...], ccols: tuple[str, ...], **kw: object
) -> list[RelationshipEdge]:
    """TABLE edge + one COLUMN edge per column pair, sharing constraint_name."""
    tbl = edge(name, LineageLevel.TABLE, parent, child, pcols, ccols, constraint=name, **kw)  # type: ignore[arg-type]
    cols = [
        edge(f"{name}:{i}", LineageLevel.COLUMN, parent, child, (p,), (c,), constraint=name, **kw)  # type: ignore[arg-type]
        for i, (p, c) in enumerate(zip(pcols, ccols, strict=True))
    ]
    return [tbl, *cols]


LTV_ART = uid("artifact:customer_ltv")
SEG_ART = uid("artifact:customer_segment")


@dataclass
class Shop:
    catalog: CatalogSnapshot
    edges: list[RelationshipEdge] = field(default_factory=list)

    def by_name(self, name: str) -> RelationshipEdge:
        target = uid(name)
        return next(e for e in self.edges if e.edge_id == target)


def make_shop() -> Shop:
    edges: list[RelationshipEdge] = []
    edges += fk_pair("fk_po_customer", "customer", "purchase_order", ("id",), ("customer_id",))
    edges += fk_pair(
        "fk_ol_order",
        "purchase_order",
        "order_line",
        ("id",),
        ("order_id",),
        on_delete=OnDeleteAction.CASCADE,
    )
    edges += fk_pair("fk_ol_product", "product", "order_line", ("id",), ("product_id",))
    edges += fk_pair(
        "fk_customer_referrer",
        "customer",
        "customer",
        ("id",),
        ("referrer_id",),
        on_delete=OnDeleteAction.SET_NULL,
    )
    edges += fk_pair(
        "inferred_newsletter",
        "customer",
        "newsletter",
        ("id",),
        ("subscriber_ref",),
        origin=EdgeOrigin.INFERRED,
        status=EdgeStatus.SUGGESTED,
        on_delete=None,
        confidence=0.97,
    )
    sql = {"origin": EdgeOrigin.SQL_LINEAGE, "on_delete": None}
    edges.append(
        edge(
            "ltv_from_customer",
            LineageLevel.TABLE,
            "customer",
            "customer_ltv",
            ("id",),
            ("customer_id",),
            transform=Transform.IDENTITY,
            artifact_id=LTV_ART,
            **sql,  # type: ignore[arg-type]
        )
    )
    edges.append(
        edge(
            "ltv_from_customer:col",
            LineageLevel.COLUMN,
            "customer",
            "customer_ltv",
            ("id",),
            ("customer_id",),
            transform=Transform.IDENTITY,
            artifact_id=LTV_ART,
            **sql,  # type: ignore[arg-type]
        )
    )
    edges.append(
        edge(
            "ltv_from_order",
            LineageLevel.TABLE,
            "purchase_order",
            "customer_ltv",
            (),
            (),
            transform=Transform.AGGREGATE,
            traceability=Traceability.AGGREGATE,
            artifact_id=LTV_ART,
            **sql,  # type: ignore[arg-type]
        )
    )
    edges.append(
        edge(
            "segment_from_ltv",
            LineageLevel.TABLE,
            "customer_ltv",
            "customer_segment",
            ("customer_id",),
            ("customer_id",),
            transform=Transform.IDENTITY,
            artifact_id=SEG_ART,
            **sql,  # type: ignore[arg-type]
        )
    )
    return Shop(catalog=make_catalog(), edges=edges)


@pytest.fixture
def shop() -> Shop:
    return make_shop()


@dataclass(frozen=True)
class Kit:
    """Helper namespace handed to tests (conftest modules are not importable by tests)."""

    t = staticmethod(t)
    uid = staticmethod(uid)
    edge = staticmethod(edge)
    fk_pair = staticmethod(fk_pair)
    make_catalog = staticmethod(make_catalog)
    make_shop = staticmethod(make_shop)
    ltv_art: UUID = LTV_ART
    seg_art: UUID = SEG_ART


@pytest.fixture
def kit() -> Kit:
    return Kit()
