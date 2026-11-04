"""PgAuditSink: hash chain validity, envelope, tamper detection, serialized appends."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from lae.audit.hashchain import GENESIS_HASH, HashChain, next_hash, verify_chain
from lae.contracts.enums import AuditEventType
from lae.contracts.ports import AuditSink
from lae.db.audit_sink import PgAuditSink
from lae.db.repositories import AuditRepo
from lae.errors import AuditChainBroken, ValidationError

pytestmark = pytest.mark.integration


def test_implements_port(db_session: Session) -> None:
    assert isinstance(PgAuditSink(db_session), AuditSink)


def test_appends_form_a_valid_chain_with_envelope(db_session: Session) -> None:
    repo = AuditRepo(db_session)
    sink = PgAuditSink(db_session, HashChain)
    head_before = repo.head()
    entity = uuid4()
    records = [
        sink.append(
            AuditEventType.REQUEST_RECEIVED,
            "erasure_request",
            entity,
            "alice",
            {"subject_key_hmac": "a" * 64},
        ),
        sink.append(
            AuditEventType.PLAN_CREATED,
            "erasure_plan",
            None,
            "bob",
            {"plan_hash": "b" * 64, "items": 3},
        ),
        sink.append("PLAN_APPROVED", "erasure_plan", entity, "carol", {}),  # type: ignore[arg-type]
    ]
    expected_prev = head_before.event_hash if head_before else GENESIS_HASH
    assert records[0].prev_hash == expected_prev
    assert records[1].prev_hash == records[0].event_hash
    assert records[0].seq < records[1].seq < records[2].seq
    for r in records:
        assert r.event_hash == next_hash(r.prev_hash, r.seq, r.event_type, r.payload_canonical)
    env = json.loads(records[0].payload_canonical)
    assert env == {
        "data": {"subject_key_hmac": "a" * 64},
        "envelope": {
            "actor": "alice",
            "entity_id": str(entity),
            "entity_type": "erasure_request",
            "occurred_at": records[0]
            .occurred_at.isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
        },
    }
    assert json.loads(records[1].payload_canonical)["envelope"]["entity_id"] is None
    # stored rows equal the returned records (columns == envelope, CR crypto-1 §1)
    stored = {e.seq: e for e in repo.chain()}
    for r in records:
        assert stored[r.seq] == r
    result = verify_chain(repo.chain())
    assert result.valid and result.head_seq == records[-1].seq
    assert repo.head() == records[-1]
    assert repo.count() >= 3
    assert set(repo.event_ids([r.seq for r in records])) == {r.seq for r in records}
    assert [e.seq for e in repo.for_entities([entity])] == [records[0].seq, records[2].seq]
    assert repo.prev_hash_before(records[1].seq) == records[0].event_hash
    export = repo.export_chain(after_seq=records[0].seq - 1)
    assert [e["seq"] for e in export] == [r.seq for r in records]
    assert set(export[0]) >= {"seq", "event_type", "payload_canonical", "prev_hash", "event_hash"}
    assert repo.list_events(after_seq=records[0].seq, limit=1)[0] == records[1]
    assert repo.get(records[2].seq) == records[2]


def test_clock_is_normalized_to_utc_and_validated(db_session: Session) -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    fixed = datetime(2026, 11, 4, 12, 0, 0, 123456, tzinfo=ist)
    sink = PgAuditSink(db_session, clock=lambda: fixed)
    rec = sink.append(AuditEventType.POLICY_CHANGED, "retention_policy", None, "ops", {})
    assert rec.occurred_at == fixed and rec.occurred_at.tzinfo == UTC
    assert '"occurred_at":"2026-11-04T06:30:00.123456Z"' in rec.payload_canonical
    with pytest.raises(ValidationError):
        PgAuditSink(db_session, clock=lambda: datetime(2026, 1, 1)).append(
            AuditEventType.POLICY_CHANGED, "x", None, "ops", {}
        )
    with pytest.raises(ValueError):
        sink.append("NOT_A_TYPE", "x", None, "ops", {})  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        sink.append(AuditEventType.POLICY_CHANGED, "", None, "ops", {})


def test_tampering_is_detected_at_exact_seq(db_session: Session) -> None:
    """AT-21: a superuser disables the trigger and rewrites history -> detected (rolled back)."""
    sink = PgAuditSink(db_session)
    recs = [
        sink.append(AuditEventType.EVIDENCE_ISSUED, "evidence_bundle", None, "svc", {"n": i})
        for i in range(3)
    ]
    db_session.execute(
        text("ALTER TABLE lae.audit_event DISABLE TRIGGER trg_audit_event_append_only")
    )
    db_session.execute(
        text("UPDATE lae.audit_event SET payload_canonical = :p WHERE seq = :s"),
        {"p": recs[1].payload_canonical.replace('"n":1', '"n":9'), "s": recs[1].seq},
    )
    result = verify_chain(AuditRepo(db_session).chain())
    assert not result.valid and result.first_invalid_seq == recs[1].seq
    # a tampered HEAD makes the next append refuse to extend the chain
    db_session.execute(
        text("UPDATE lae.audit_event SET actor = 'x', event_type = 'PLAN_CREATED' WHERE seq = :s"),
        {"s": recs[2].seq},
    )
    with pytest.raises(AuditChainBroken):
        sink.append(AuditEventType.CHAIN_HEAD_EXPORTED, "audit", None, "svc", {})


def test_payload_must_be_canonicalizable(db_session: Session) -> None:
    from lae.errors import CanonicalizationError

    with pytest.raises(CanonicalizationError):
        PgAuditSink(db_session).append(AuditEventType.PLAN_CREATED, "p", None, "a", {"x": 1.5})


def test_concurrent_appends_are_serialized(migrated_db: Engine) -> None:
    """Session B blocks on the advisory lock until A commits, then chains onto A's event."""
    a = Session(bind=migrated_db)
    b_result: dict[str, object] = {}
    try:
        rec_a = PgAuditSink(a).append(
            AuditEventType.SNAPSHOT_CREATED, "lineage_snapshot", None, "writer-a", {"t": "a"}
        )

        def run_b() -> None:
            with Session(bind=migrated_db) as b:
                b.execute(text("SET LOCAL lock_timeout = '10s'"))
                b_result["rec"] = PgAuditSink(b).append(
                    AuditEventType.SNAPSHOT_CREATED,
                    "lineage_snapshot",
                    None,
                    "writer-b",
                    {"t": "b"},
                )
                b.commit()

        thread = threading.Thread(target=run_b)
        thread.start()
        time.sleep(0.5)
        assert "rec" not in b_result  # still waiting for A's transaction-level lock
        a.commit()
        thread.join(timeout=15)
        assert not thread.is_alive()
    finally:
        a.close()
    rec_b = b_result["rec"]
    assert rec_b.prev_hash == rec_a.event_hash  # type: ignore[attr-defined]
    with Session(bind=migrated_db) as s:
        assert verify_chain(AuditRepo(s).chain()).valid
