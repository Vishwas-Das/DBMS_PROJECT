"""`PgAuditSink`: appends to the `lae.audit_event` hash chain in the CALLER's transaction.

Per append (architecture.md §13, CR verification-crypto-engineer-1 §1):
1. `pg_advisory_xact_lock(hashtext('lae.audit_chain'))` serializes appenders until commit/rollback;
2. the head event is read and its own hash re-verified (`AuditChainBroken` if it does not verify);
3. `seq` is reserved with nextval() so it can be hashed before the INSERT
   (`OVERRIDING SYSTEM VALUE`; the identity stays GENERATED ALWAYS for everyone else);
4. `payload_canonical = canonical_json({"envelope": {entity_type, entity_id, actor, occurred_at},
   "data": payload})` and `event_hash = hashchain.next_hash(prev, seq, event_type, payload)`;
5. INSERT. Nothing is committed here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from lae.audit import hashchain
from lae.audit.canonical import canonical_json
from lae.contracts.enums import AuditEventType
from lae.contracts.types import AuditEventRecord
from lae.errors import AuditChainBroken, ValidationError

CHAIN_LOCK_KEY = "lae.audit_chain"

_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtext(:k))")
_HEAD_SQL = text(
    "SELECT seq, event_type, payload_canonical, prev_hash, event_hash "
    "FROM lae.audit_event ORDER BY seq DESC LIMIT 1"
)
_NEXTVAL_SQL = text("SELECT nextval(pg_get_serial_sequence('lae.audit_event', 'seq'))")
_INSERT_SQL = text(
    "INSERT INTO lae.audit_event (seq, event_id, occurred_at, event_type, entity_type, "
    "entity_id, actor, payload_canonical, prev_hash, event_hash) OVERRIDING SYSTEM VALUE "
    "VALUES (:seq, :event_id, :occurred_at, :event_type, :entity_type, :entity_id, :actor, "
    ":payload_canonical, :prev_hash, :event_hash)"
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def build_payload_canonical(
    entity_type: str,
    entity_id: UUID | None,
    actor: str,
    occurred_at: datetime,
    payload: Mapping[str, Any],
) -> str:
    """The exact text that is stored and hashed (envelope + data)."""
    return canonical_json(
        {
            "envelope": {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "actor": actor,
                "occurred_at": occurred_at,
            },
            "data": dict(payload),
        }
    ).decode("utf-8")


class PgAuditSink:
    """AuditSink over a SQLAlchemy Session. Never commits; the service owns the transaction."""

    def __init__(
        self,
        session: Session,
        chain: type[hashchain.HashChain] | hashchain.HashChain = hashchain.HashChain,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._session = session
        self._chain = chain
        self._clock = clock

    def append(
        self,
        event_type: AuditEventType,
        entity_type: str,
        entity_id: UUID | None,
        actor: str,
        payload: Mapping[str, Any],
    ) -> AuditEventRecord:
        etype = AuditEventType(event_type)  # ValueError for an unknown type
        if not entity_type or not actor:
            raise ValidationError("audit events need an entity_type and an actor")
        occurred_at = self._clock()
        if occurred_at.tzinfo is None:
            raise ValidationError("audit clock must return timezone-aware datetimes")
        # Microsecond precision is what timestamptz stores, so the envelope round-trips exactly.
        occurred_at = occurred_at.astimezone(UTC)
        payload_canonical = build_payload_canonical(
            entity_type, entity_id, actor, occurred_at, payload
        )

        s = self._session
        s.execute(_LOCK_SQL, {"k": CHAIN_LOCK_KEY})
        head = s.execute(_HEAD_SQL).one_or_none()
        if head is None:
            prev_hash = self._chain.genesis()
            head_seq = 0
        else:
            head_seq, h_type, h_payload, h_prev, h_hash = head
            try:
                recomputed = self._chain.next_hash(h_prev, int(head_seq), h_type, h_payload)
            except ValueError:
                recomputed = ""
            if recomputed != h_hash:
                raise AuditChainBroken(
                    "audit chain head does not verify", details={"head_seq": int(head_seq)}
                )
            prev_hash = h_hash
        seq = int(s.execute(_NEXTVAL_SQL).scalar_one())
        if seq <= int(head_seq):
            raise AuditChainBroken("audit sequence is behind the chain head", details={"seq": seq})
        event_hash = self._chain.next_hash(prev_hash, seq, etype.value, payload_canonical)
        s.execute(
            _INSERT_SQL,
            {
                "seq": seq,
                "event_id": uuid4(),
                "occurred_at": occurred_at,
                "event_type": etype.value,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "actor": actor,
                "payload_canonical": payload_canonical,
                "prev_hash": prev_hash,
                "event_hash": event_hash,
            },
        )
        return AuditEventRecord(
            seq=seq,
            event_type=etype.value,
            entity_type=entity_type,
            entity_id=entity_id,
            actor=actor,
            occurred_at=occurred_at,
            payload_canonical=payload_canonical,
            prev_hash=prev_hash,
            event_hash=event_hash,
        )
