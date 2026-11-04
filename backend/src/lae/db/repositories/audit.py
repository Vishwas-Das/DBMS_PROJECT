"""AuditRepo: read side of the append-only audit chain (writes go through PgAuditSink)."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from lae.contracts.types import AuditEventRecord
from lae.db.models import AuditEvent
from lae.db.repositories._base import Repo
from lae.errors import NotFoundError


def _event(m: AuditEvent) -> AuditEventRecord:
    return AuditEventRecord(
        seq=m.seq,
        event_type=m.event_type,
        entity_type=m.entity_type,
        entity_id=m.entity_id,
        actor=m.actor,
        occurred_at=m.occurred_at,
        payload_canonical=m.payload_canonical,
        prev_hash=m.prev_hash,
        event_hash=m.event_hash,
    )


class AuditRepo(Repo):
    """Methods (read-only; there is deliberately no update/delete):
    list_events(after_seq=0, limit=100) -> list[AuditEventRecord]          (ascending seq)
    chain(after_seq=0, up_to_seq=None) -> list[AuditEventRecord]           (for verify_chain)
    head() -> AuditEventRecord | None
    get(seq) -> AuditEventRecord
    prev_hash_before(seq) -> str | None    (event_hash of the event preceding seq, if any)
    for_entities(entity_ids, entity_type=None) -> list[AuditEventRecord]
    event_ids(seqs) -> dict[int, UUID]
    count() -> int
    export_chain(after_seq=0, up_to_seq=None) -> list[dict]   (CR crypto-1 §3 chain export)
    """

    def list_events(self, after_seq: int = 0, limit: int = 100) -> list[AuditEventRecord]:
        q = (
            select(AuditEvent)
            .where(AuditEvent.seq > after_seq)
            .order_by(AuditEvent.seq)
            .limit(limit)
        )
        return [_event(m) for m in self.session.scalars(q)]

    def chain(self, after_seq: int = 0, up_to_seq: int | None = None) -> list[AuditEventRecord]:
        q = select(AuditEvent).where(AuditEvent.seq > after_seq)
        if up_to_seq is not None:
            q = q.where(AuditEvent.seq <= up_to_seq)
        return [_event(m) for m in self.session.scalars(q.order_by(AuditEvent.seq))]

    def head(self) -> AuditEventRecord | None:
        m = self.session.scalars(
            select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(1)
        ).first()
        return None if m is None else _event(m)

    def get(self, seq: int) -> AuditEventRecord:
        m = self.session.get(AuditEvent, seq)
        if m is None:
            raise NotFoundError("audit_event not found", details={"seq": seq})
        return _event(m)

    def prev_hash_before(self, seq: int) -> str | None:
        q = (
            select(AuditEvent.event_hash)
            .where(AuditEvent.seq < seq)
            .order_by(AuditEvent.seq.desc())
            .limit(1)
        )
        return self.session.scalars(q).first()

    def for_entities(
        self, entity_ids: Collection[UUID], entity_type: str | None = None
    ) -> list[AuditEventRecord]:
        if not entity_ids:
            return []
        q = select(AuditEvent).where(AuditEvent.entity_id.in_(list(entity_ids)))
        if entity_type is not None:
            q = q.where(AuditEvent.entity_type == entity_type)
        return [_event(m) for m in self.session.scalars(q.order_by(AuditEvent.seq))]

    def event_ids(self, seqs: Collection[int]) -> dict[int, UUID]:
        if not seqs:
            return {}
        q = select(AuditEvent.seq, AuditEvent.event_id).where(AuditEvent.seq.in_(list(seqs)))
        return {int(s): e for s, e in self.session.execute(q)}

    def count(self) -> int:
        return int(self.session.execute(select(func.count()).select_from(AuditEvent)).scalar_one())

    def export_chain(
        self, after_seq: int = 0, up_to_seq: int | None = None
    ) -> list[dict[str, Any]]:
        return [
            {
                "seq": e.seq,
                "event_type": e.event_type,
                "entity_type": e.entity_type,
                "entity_id": None if e.entity_id is None else str(e.entity_id),
                "actor": e.actor,
                "occurred_at": e.occurred_at.isoformat().replace("+00:00", "Z"),
                "payload_canonical": e.payload_canonical,
                "prev_hash": e.prev_hash,
                "event_hash": e.event_hash,
            }
            for e in self.chain(after_seq, up_to_seq)
        ]
