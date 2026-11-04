"""RequestRepo: erasure requests and their residual probes."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from lae.contracts.enums import Normalization, RequestStatus
from lae.contracts.types import ColumnRef, KeyTuple, ResidualProbe, TableRef
from lae.db.models import ErasureRequest, ResidualProbeModel
from lae.db.records import RequestRecord
from lae.db.repositories._base import Repo, key_from_json, key_to_json


def _record(m: ErasureRequest) -> RequestRecord:
    return RequestRecord(
        id=m.id,
        subject_root_id=m.subject_root_id,
        subject_key_hmac=m.subject_key_hmac,
        status=RequestStatus(m.status),
        requested_by=m.requested_by,
        reason=m.reason,
        created_at=m.created_at,
        completed_at=m.completed_at,
        has_subject_key=m.subject_key is not None,
    )


class RequestRepo(Repo):
    """Methods:
    create(subject_root_id, subject_key, subject_key_hmac, requested_by, reason=None)
        -> RequestRecord   (status RECEIVED)
    get(request_id) -> RequestRecord
    list_requests(limit=50, offset=0, status=None) -> list[RequestRecord]        (newest first)
    find_by_hmac(subject_root_id, subject_key_hmac) -> list[RequestRecord]
    count_by_status() -> dict[RequestStatus, int]
    set_status(request_id, status, *, completed_at=None) -> RequestRecord
    subject_key(request_id) -> KeyTuple | None        (raw key; None once purged)
    purge_subject_key(request_id) -> RequestRecord    (only allowed in VERIFIED / CANCELLED)
    add_probes(request_id, probes) -> int             (duplicates ignored)
    list_probes(request_id) -> list[ResidualProbe]    (sorted)
    """

    def create(
        self,
        subject_root_id: UUID,
        subject_key: KeyTuple,
        subject_key_hmac: str,
        requested_by: str,
        reason: str | None = None,
    ) -> RequestRecord:
        m = ErasureRequest(
            subject_root_id=subject_root_id,
            subject_key=key_to_json(subject_key),
            subject_key_hmac=subject_key_hmac,
            status=RequestStatus.RECEIVED.value,
            requested_by=requested_by,
            reason=reason,
        )
        self.session.add(m)
        self.session.flush()
        self.session.refresh(m)
        return _record(m)

    def get(self, request_id: UUID) -> RequestRecord:
        return _record(self._get(ErasureRequest, request_id))

    def list_requests(
        self, limit: int = 50, offset: int = 0, status: RequestStatus | None = None
    ) -> list[RequestRecord]:
        q = select(ErasureRequest).order_by(
            ErasureRequest.created_at.desc(), ErasureRequest.id.desc()
        )
        if status is not None:
            q = q.where(ErasureRequest.status == RequestStatus(status).value)
        return [_record(m) for m in self.session.scalars(q.limit(limit).offset(offset))]

    def find_by_hmac(self, subject_root_id: UUID, subject_key_hmac: str) -> list[RequestRecord]:
        q = (
            select(ErasureRequest)
            .where(
                ErasureRequest.subject_root_id == subject_root_id,
                ErasureRequest.subject_key_hmac == subject_key_hmac,
            )
            .order_by(ErasureRequest.created_at.desc(), ErasureRequest.id.desc())
        )
        return [_record(m) for m in self.session.scalars(q)]

    def count_by_status(self) -> dict[RequestStatus, int]:
        q = select(ErasureRequest.status, func.count()).group_by(ErasureRequest.status)
        counts = {s: 0 for s in RequestStatus}
        for status, n in self.session.execute(q):
            counts[RequestStatus(status)] = int(n)
        return counts

    def set_status(
        self, request_id: UUID, status: RequestStatus, *, completed_at: datetime | None = None
    ) -> RequestRecord:
        m = self._get(ErasureRequest, request_id)
        m.status = RequestStatus(status).value
        if completed_at is not None:
            m.completed_at = completed_at
        self.session.flush()
        return _record(m)

    def subject_key(self, request_id: UUID) -> KeyTuple | None:
        m = self._get(ErasureRequest, request_id)
        return None if m.subject_key is None else key_from_json(m.subject_key)

    def purge_subject_key(self, request_id: UUID) -> RequestRecord:
        m = self._get(ErasureRequest, request_id)
        m.subject_key = None
        self.session.flush()
        return _record(m)

    def add_probes(self, request_id: UUID, probes: Sequence[ResidualProbe]) -> int:
        if not probes:
            return 0
        rows = [
            {
                "request_id": request_id,
                "origin_schema": p.origin.table.schema,
                "origin_table": p.origin.table.name,
                "origin_column": p.origin.column,
                "normalization": Normalization(p.normalization).value,
                "value_hmac": p.value_hmac,
            }
            for p in probes
        ]
        stmt = (
            insert(ResidualProbeModel)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["request_id", "value_hmac", "normalization"])
            .returning(ResidualProbeModel.id)
        )
        return len(self.session.execute(stmt).all())

    def list_probes(self, request_id: UUID) -> list[ResidualProbe]:
        q = select(ResidualProbeModel).where(ResidualProbeModel.request_id == request_id)
        probes = [
            ResidualProbe(
                origin=ColumnRef(TableRef(m.origin_schema, m.origin_table), m.origin_column),
                normalization=Normalization(m.normalization),
                value_hmac=m.value_hmac,
            )
            for m in self.session.scalars(q)
        ]
        probes.sort(key=lambda p: (p.origin, p.normalization.value, p.value_hmac))
        return probes
