"""SnapshotRepo: catalog snapshots, catalog tables/columns and lineage edges."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import aliased, selectinload

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
from lae.contracts.types import (
    CatalogSnapshot,
    ColumnInfo,
    RelationshipEdge,
    TableInfo,
    TableRef,
)
from lae.db.models import CatalogColumn, CatalogTable, LineageEdge, LineageSnapshot
from lae.db.records import SnapshotRecord
from lae.db.repositories._base import Repo, confidence_to_db
from lae.errors import NotFoundError, ValidationError


def _snapshot(m: LineageSnapshot) -> SnapshotRecord:
    return SnapshotRecord(
        id=m.id,
        created_at=m.created_at,
        status=SnapshotStatus(m.status),
        scanned_schemas=tuple(m.scanned_schemas),
        schema_fingerprint=m.schema_fingerprint,
        table_count=m.table_count,
        edge_count=m.edge_count,
        warnings=tuple(m.warnings or ()),
    )


def _edge(m: LineageEdge, source: TableRef, target: TableRef) -> RelationshipEdge:
    return RelationshipEdge(
        edge_id=m.id,
        level=LineageLevel(m.level),
        origin=EdgeOrigin(m.origin),
        source=source,
        target=target,
        source_columns=tuple(m.source_columns),
        target_columns=tuple(m.target_columns),
        transform=Transform(m.transform),
        traceability=Traceability(m.traceability),
        status=EdgeStatus(m.status),
        on_delete=None if m.on_delete_action is None else OnDeleteAction(m.on_delete_action),
        constraint_name=m.constraint_name,
        confidence=float(m.confidence),
        artifact_id=m.artifact_id,
        details=dict(m.details or {}),
    )


class SnapshotRepo(Repo):
    """Methods:
    create(scanned_schemas, schema_fingerprint, status=BUILDING) -> SnapshotRecord
    get(snapshot_id) -> SnapshotRecord
    latest(status=READY) -> SnapshotRecord | None
    list_snapshots(limit=50) -> list[SnapshotRecord]                       (newest first)
    set_status(snapshot_id, status, *, schema_fingerprint, table_count, edge_count, warnings)
    save_catalog(snapshot_id, catalog) -> dict[TableRef, UUID]
    load_catalog(snapshot_id) -> CatalogSnapshot
    table_ids(snapshot_id) -> dict[TableRef, UUID]
    save_edges(snapshot_id, edges) -> int
    load_edges(snapshot_id, *, statuses=None, level=None) -> list[RelationshipEdge]
    get_edge(edge_id) -> RelationshipEdge
    set_edge_status(edge_id, status) -> RelationshipEdge
    """

    def create(
        self,
        scanned_schemas: Sequence[str],
        schema_fingerprint: str,
        status: SnapshotStatus = SnapshotStatus.BUILDING,
    ) -> SnapshotRecord:
        m = LineageSnapshot(
            scanned_schemas=list(scanned_schemas),
            schema_fingerprint=schema_fingerprint,
            status=SnapshotStatus(status).value,
        )
        self.session.add(m)
        self.session.flush()
        self.session.refresh(m)
        return _snapshot(m)

    def get(self, snapshot_id: UUID) -> SnapshotRecord:
        return _snapshot(self._get(LineageSnapshot, snapshot_id))

    def latest(self, status: SnapshotStatus | None = SnapshotStatus.READY) -> SnapshotRecord | None:
        q = select(LineageSnapshot).order_by(
            LineageSnapshot.created_at.desc(), LineageSnapshot.id.desc()
        )
        if status is not None:
            q = q.where(LineageSnapshot.status == SnapshotStatus(status).value)
        m = self.session.scalars(q.limit(1)).first()
        return None if m is None else _snapshot(m)

    def list_snapshots(self, limit: int = 50) -> list[SnapshotRecord]:
        q = (
            select(LineageSnapshot)
            .order_by(LineageSnapshot.created_at.desc(), LineageSnapshot.id.desc())
            .limit(limit)
        )
        return [_snapshot(m) for m in self.session.scalars(q)]

    def set_status(
        self,
        snapshot_id: UUID,
        status: SnapshotStatus,
        *,
        schema_fingerprint: str | None = None,
        table_count: int | None = None,
        edge_count: int | None = None,
        warnings: Sequence[Any] | None = None,
    ) -> SnapshotRecord:
        m = self._get(LineageSnapshot, snapshot_id)
        m.status = SnapshotStatus(status).value
        if schema_fingerprint is not None:
            m.schema_fingerprint = schema_fingerprint
        if table_count is not None:
            m.table_count = table_count
        if edge_count is not None:
            m.edge_count = edge_count
        if warnings is not None:
            m.warnings = list(warnings)
        self.session.flush()
        return _snapshot(m)

    # ------------------------------------------------------------ catalog
    def save_catalog(self, snapshot_id: UUID, catalog: CatalogSnapshot) -> dict[TableRef, UUID]:
        self._get(LineageSnapshot, snapshot_id)
        models: dict[TableRef, CatalogTable] = {}
        for ref in sorted(catalog.tables):
            info = catalog.tables[ref]
            t = CatalogTable(
                snapshot_id=snapshot_id,
                schema_name=ref.schema,
                table_name=ref.name,
                kind=TableKind(info.kind).value,
                primary_key_columns=list(info.primary_key),
                estimated_rows=info.estimated_rows,
                definition_sql=info.definition_sql,
            )
            t.columns = [
                CatalogColumn(
                    column_name=c.name,
                    ordinal_position=c.ordinal,
                    data_type=c.data_type,
                    is_nullable=c.is_nullable,
                )
                for c in info.columns
            ]
            self.session.add(t)
            models[ref] = t
        self.session.flush()
        return {ref: t.id for ref, t in models.items()}

    def table_ids(self, snapshot_id: UUID) -> dict[TableRef, UUID]:
        q = select(CatalogTable.schema_name, CatalogTable.table_name, CatalogTable.id).where(
            CatalogTable.snapshot_id == snapshot_id
        )
        return {TableRef(s, n): i for s, n, i in self.session.execute(q)}

    def load_catalog(self, snapshot_id: UUID) -> CatalogSnapshot:
        snap = self._get(LineageSnapshot, snapshot_id)
        q = (
            select(CatalogTable)
            .where(CatalogTable.snapshot_id == snapshot_id)
            .options(selectinload(CatalogTable.columns))
            .order_by(CatalogTable.schema_name, CatalogTable.table_name)
        )
        tables: dict[TableRef, TableInfo] = {}
        for t in self.session.scalars(q):
            ref = TableRef(t.schema_name, t.table_name)
            tables[ref] = TableInfo(
                ref=ref,
                kind=TableKind(t.kind),
                columns=tuple(
                    ColumnInfo(c.column_name, c.ordinal_position, c.data_type, c.is_nullable)
                    for c in sorted(t.columns, key=lambda c: c.ordinal_position)
                ),
                primary_key=tuple(t.primary_key_columns),
                estimated_rows=t.estimated_rows,
                definition_sql=t.definition_sql,
            )
        return CatalogSnapshot(tables, tuple(snap.scanned_schemas), snap.schema_fingerprint)

    # ------------------------------------------------------------ edges
    def save_edges(self, snapshot_id: UUID, edges: Sequence[RelationshipEdge]) -> int:
        ids = self.table_ids(snapshot_id)
        for e in edges:
            try:
                src, tgt = ids[e.source], ids[e.target]
            except KeyError as exc:
                raise ValidationError(
                    "edge references a table that is not in the snapshot",
                    details={"table": str(exc.args[0])},
                ) from None
            self.session.add(
                LineageEdge(
                    id=e.edge_id,
                    snapshot_id=snapshot_id,
                    level=LineageLevel(e.level).value,
                    origin=EdgeOrigin(e.origin).value,
                    source_table_id=src,
                    target_table_id=tgt,
                    source_columns=list(e.source_columns),
                    target_columns=list(e.target_columns),
                    transform=Transform(e.transform).value,
                    traceability=Traceability(e.traceability).value,
                    on_delete_action=None if e.on_delete is None else e.on_delete.value,
                    constraint_name=e.constraint_name,
                    confidence=confidence_to_db(e.confidence),
                    status=EdgeStatus(e.status).value,
                    artifact_id=e.artifact_id,
                    details=dict(e.details),
                )
            )
        self.session.flush()
        return len(edges)

    def _edge_query(self) -> Any:
        src = aliased(CatalogTable)
        tgt = aliased(CatalogTable)
        q = (
            select(
                LineageEdge,
                src.schema_name,
                src.table_name,
                tgt.schema_name,
                tgt.table_name,
            )
            .join(src, src.id == LineageEdge.source_table_id)
            .join(tgt, tgt.id == LineageEdge.target_table_id)
        )
        return q

    def load_edges(
        self,
        snapshot_id: UUID,
        *,
        statuses: Collection[EdgeStatus] | None = None,
        level: LineageLevel | None = None,
    ) -> list[RelationshipEdge]:
        q = self._edge_query().where(LineageEdge.snapshot_id == snapshot_id)
        if statuses is not None:
            q = q.where(LineageEdge.status.in_([EdgeStatus(s).value for s in statuses]))
        if level is not None:
            q = q.where(LineageEdge.level == LineageLevel(level).value)
        out = [
            _edge(m, TableRef(ss, sn), TableRef(ts, tn))
            for m, ss, sn, ts, tn in self.session.execute(q)
        ]
        out.sort(
            key=lambda e: (
                e.level.value,
                e.source,
                e.target,
                e.source_columns,
                e.target_columns,
                str(e.edge_id),
            )
        )
        return out

    def get_edge(self, edge_id: UUID) -> RelationshipEdge:
        row = self.session.execute(self._edge_query().where(LineageEdge.id == edge_id)).first()
        if row is None:
            raise NotFoundError(
                "lineage_edge not found", details={"entity": "lineage_edge", "id": str(edge_id)}
            )
        m, ss, sn, ts, tn = row
        return _edge(m, TableRef(ss, sn), TableRef(ts, tn))

    def set_edge_status(self, edge_id: UUID, status: EdgeStatus) -> RelationshipEdge:
        m = self._get(LineageEdge, edge_id)
        m.status = EdgeStatus(status).value
        self.session.flush()
        return self.get_edge(edge_id)
