"""DB-backed audit chain -> signed evidence -> OFFLINE verification (AT-20 / AT-21 style).

Flow: verify a real erasure (rolled back), append its audit events through ``PgAuditSink`` in
real committed transactions, export the chain with ``AuditRepo.export_chain``, build and sign
the evidence bundle, export a signed chain-head anchor, write everything to files, then verify
the files with ``verify_evidence_offline`` / ``verify_anchor`` while every route to the database
and the network is blocked.

Tampering: a superuser disables the append-only trigger and rewrites a stored row inside a
transaction that is ROLLED BACK (nothing tampered is ever committed). A fresh export taken in
that transaction fails offline verification at the exact ``seq``.
"""

from __future__ import annotations

import contextlib
import json
import socket
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from lae.audit.anchor import export_chain_head, verify_anchor
from lae.audit.evidence import (
    ChainAnchor,
    ExecutionEvidence,
    build_evidence,
    leaves_from_verification,
    verify_evidence_offline,
)
from lae.audit.hashchain import GENESIS_HASH, verify_chain
from lae.audit.merkle import MerkleTree, verify_proof
from lae.audit.signing import Signer
from lae.contracts.enums import AuditEventType, LineageLevel, VerificationOutcome
from lae.contracts.types import AuditEventRecord, RecordKey, TableRef
from lae.db.audit_sink import PgAuditSink
from lae.db.repositories import AuditRepo

pytestmark = pytest.mark.integration

DISABLE_TRIGGER = "ALTER TABLE lae.audit_event DISABLE TRIGGER trg_audit_event_append_only"
ENABLE_TRIGGER = "ALTER TABLE lae.audit_event ENABLE TRIGGER trg_audit_event_append_only"
ACTOR = "verification-integration"


class _NoNetwork(RuntimeError):
    pass


def _blocked(*_a: Any, **_k: Any) -> Any:
    raise _NoNetwork("network / database access attempted during offline verification")


@contextlib.contextmanager
def offline(mp: pytest.MonkeyPatch, engine: Engine) -> Iterator[None]:
    """Close the pool and make every DB / socket connection attempt raise."""
    engine.dispose()
    with mp.context() as m:
        m.setattr(psycopg, "connect", _blocked)
        m.setattr(psycopg.Connection, "connect", classmethod(_blocked))
        m.setattr(socket.socket, "connect", _blocked)
        m.setattr(socket, "create_connection", _blocked)
        with pytest.raises(_NoNetwork):  # the block is effective
            engine.connect()
        yield


def append_run_events(engine: Engine, w: Any, plan: Any, report: Any) -> list[AuditEventRecord]:
    """The events a real run would record (payloads hold hashes and counts only)."""
    subject_hmac = w.keyring.key_digest(
        RecordKey(TableRef("app", "customer"), ("id",), (w.subject,))
    )
    out: list[AuditEventRecord] = []
    steps: list[tuple[AuditEventType, str, Any, dict[str, Any]]] = [
        (AuditEventType.REQUEST_RECEIVED, "erasure_request", plan.request_id,
         {"subject_key_hmac": subject_hmac}),
        (AuditEventType.PLAN_CREATED, "erasure_plan", plan.plan_id,
         {"plan_hash": plan.plan_hash, "items": len(plan.items)}),
        (AuditEventType.PLAN_APPROVED, "erasure_plan", plan.plan_id,
         {"plan_hash": plan.plan_hash}),
        (AuditEventType.EXECUTION_COMMITTED, "execution_run", uuid4(),
         {"plan_hash": plan.plan_hash, "steps": len(plan.items)}),
        (AuditEventType.VERIFICATION_COMPLETED, "verification_run", uuid4(),
         {"outcome": report.outcome.value, "checks": len(report.checks),
          "verified_artifacts": report.verified_artifacts,
          "required_artifacts": report.required_artifacts}),
    ]  # fmt: skip
    for event_type, entity_type, entity_id, payload in steps:
        with Session(engine) as s:  # one committed transaction per state change
            out.append(PgAuditSink(s).append(event_type, entity_type, entity_id, ACTOR, payload))
            s.commit()
    return out


def export(engine: Engine) -> str:
    with Session(engine) as s:
        return json.dumps(AuditRepo(s).export_chain())


@pytest.fixture
def run_artifacts(world: Any, migrated_db: Engine, tmp_path: Path) -> dict[str, Any]:
    w = world
    raws = [v for v in w.q("SELECT full_name, email, phone FROM app.customer WHERE id = :c",
                           c=w.subject)[0] if v]  # fmt: skip
    plan = w.subject_plan(hidden_copies=True)
    probes, scan = w.capture_probes(), w.scan_columns()
    w.apply(plan)
    report = w.verify(plan, probes=probes, scan=scan, rediscover=w.rediscover)
    assert report.outcome is VerificationOutcome.VERIFIED

    events = append_run_events(migrated_db, w, plan, report)
    signer = Signer.generate()
    issued_at = datetime.now(UTC)
    leaves = leaves_from_verification(plan, report, w.keyring)
    bundle = build_evidence(
        plan,
        ExecutionEvidence(uuid4(), issued_at, w.execution(plan)),
        report,
        leaves,
        ChainAnchor.from_event(events[-1]),
        signer,
        issued_at,
    )
    with Session(migrated_db) as s:
        issued = PgAuditSink(s).append(
            AuditEventType.EVIDENCE_ISSUED, "evidence_bundle", uuid4(), ACTOR,
            {"merkle_root": bundle.merkle_root, "leaf_count": bundle.leaf_count},
        )  # fmt: skip
        s.commit()
    with Session(migrated_db) as s:
        head = AuditRepo(s).head()
    assert head is not None and head.seq == issued.seq
    anchor = export_chain_head(head, signer, datetime.now(UTC))

    files = {
        "evidence": tmp_path / "evidence.json",
        "leaves": tmp_path / "leaves.json",
        "chain": tmp_path / "chain.json",
        "anchor": tmp_path / "anchor.json",
        "pub": tmp_path / "signing_key.pub.pem",
    }
    files["evidence"].write_text(bundle.export_json(), encoding="utf-8")
    files["leaves"].write_text(bundle.leaves_canonical, encoding="utf-8")
    files["chain"].write_text(export(migrated_db), encoding="utf-8")
    files["anchor"].write_text(anchor.export_json(), encoding="utf-8")
    files["pub"].write_text(signer.public_key_pem, encoding="utf-8")
    return {
        "files": files,
        "events": [*events, issued],
        "bundle": bundle,
        "plan": plan,
        "raws": raws,
    }


def read(files: dict[str, Path]) -> dict[str, str]:
    return {k: p.read_text(encoding="utf-8") for k, p in files.items()}


def test_db_backed_evidence_verifies_offline(
    run_artifacts: dict[str, Any], migrated_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ra = run_artifacts
    data = read(ra["files"])
    chain = json.loads(data["chain"])
    assert chain[0]["prev_hash"] == GENESIS_HASH  # full export from genesis
    assert [e["seq"] for e in chain][-6:] == [e.seq for e in ra["events"]]

    with offline(monkeypatch, migrated_db):
        ev = verify_evidence_offline(data["evidence"], data["pub"], data["leaves"], data["chain"])
        an = verify_anchor(data["anchor"], data["pub"], data["chain"])
        # a chain segment (not from genesis) that contains the anchor also verifies
        segment = json.dumps(chain[-3:])
        ev_seg = verify_evidence_offline(data["evidence"], data["pub"], chain_json=segment)
    assert ev.valid and ev.signature_ok and ev.merkle_ok and ev.chain_ok, ev.errors
    assert an.valid and an.chain_ok and an.seq == ra["events"][-1].seq, an.errors
    assert ev_seg.valid and ev_seg.chain_ok, ev_seg.errors

    bundle = json.loads(data["evidence"])["bundle"]
    assert bundle["chain_anchor"] == {
        "seq": ra["events"][-2].seq,  # the VERIFICATION_COMPLETED event at issuance
        "event_hash": ra["events"][-2].event_hash,
    }
    assert bundle["leaf_count"] == sum(
        1 if i.lineage_level is LineageLevel.TABLE else len(i.record_keys) for i in ra["plan"].items
    )
    assert bundle["verification"]["status"] == "PASSED"

    # inclusion proof of one record leaf against the signed root
    leaves = ra["bundle"].leaves
    tree = MerkleTree.build(leaves)
    assert tree.root == bundle["merkle_root"]
    assert verify_proof(leaves[3], tree.proof(3), bundle["merkle_root"])

    # no personal data in anything that leaves the database
    for name in ("evidence", "leaves", "chain", "anchor"):
        for raw in ra["raws"]:
            assert raw not in data[name], name
            assert raw.lower() not in data[name].lower(), name


def _tampered_export(engine: Engine, sql: str, params: dict[str, Any]) -> str:
    """Superuser rewrites a stored row (trigger disabled), exports, then ROLLS BACK."""
    s = Session(engine)
    try:
        s.execute(text(DISABLE_TRIGGER))
        s.execute(text(sql), params)
        s.execute(text(ENABLE_TRIGGER))
        return json.dumps(AuditRepo(s).export_chain())
    finally:
        s.rollback()
        s.close()


def test_tampered_payload_fails_offline_at_exact_seq(
    run_artifacts: dict[str, Any], migrated_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ra = run_artifacts
    approved = ra["events"][2]
    assert approved.event_type == "PLAN_APPROVED"
    forged = approved.payload_canonical.replace(ra["plan"].plan_hash, "0" * 64)
    assert forged != approved.payload_canonical
    tampered = _tampered_export(
        migrated_db,
        "UPDATE lae.audit_event SET payload_canonical = :p WHERE seq = :s",
        {"p": forged, "s": approved.seq},
    )
    data = read(ra["files"])
    with offline(monkeypatch, migrated_db):
        ev = verify_evidence_offline(data["evidence"], data["pub"], data["leaves"], tampered)
        an = verify_anchor(data["anchor"], data["pub"], tampered)
    want = f"audit chain invalid at seq {approved.seq} (event_hash_mismatch)"
    assert not ev.valid and ev.signature_ok and ev.merkle_ok and ev.chain_ok is False
    assert ev.errors == (want,)
    assert not an.valid and an.signature_ok and an.chain_ok is False and an.errors == (want,)

    # nothing was committed: a fresh export verifies again
    restored = export(migrated_db)
    assert verify_evidence_offline(data["evidence"], data["pub"], chain_json=restored).valid


def test_tampered_envelope_column_fails_offline_at_exact_seq(
    run_artifacts: dict[str, Any], migrated_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`actor` / `occurred_at` columns are not hashed; the hashed envelope still exposes them."""
    ra = run_artifacts
    target = ra["events"][3]
    data = read(ra["files"])
    for sql, params in (
        ("UPDATE lae.audit_event SET actor = :a WHERE seq = :s", {"a": "mallory"}),
        (
            "UPDATE lae.audit_event SET occurred_at = occurred_at - interval '1 day' "
            "WHERE seq = :s",
            {},
        ),
    ):
        tampered = _tampered_export(migrated_db, sql, {**params, "s": target.seq})
        # the hash chain alone cannot see this rewrite ...
        assert verify_chain(_records(tampered)).valid
        with offline(monkeypatch, migrated_db):
            ev = verify_evidence_offline(data["evidence"], data["pub"], chain_json=tampered)
            an = verify_anchor(data["anchor"], data["pub"], tampered)
        # ... the envelope cross-check does, at the exact seq
        want = (f"audit chain invalid at seq {target.seq} (envelope_mismatch)",)
        assert ev.chain_ok is False and ev.errors == want
        assert an.chain_ok is False and an.errors == want


def _records(chain_json: str) -> list[AuditEventRecord]:
    return [
        AuditEventRecord(
            e["seq"],
            e["event_type"],
            e["entity_type"],
            None,
            e["actor"],
            datetime.fromisoformat(e["occurred_at"]),
            e["payload_canonical"],
            e["prev_hash"],
            e["event_hash"],
        )
        for e in json.loads(chain_json)
    ]


def test_forged_bundle_and_wrong_key_fail(
    run_artifacts: dict[str, Any], migrated_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ra = run_artifacts
    data = read(ra["files"])
    export_doc = json.loads(data["evidence"])
    export_doc["bundle"]["verification"]["status"] = "FAILED"
    forged = json.dumps(export_doc)
    leaves = json.loads(data["leaves"])
    leaves[0]["passed"] = not leaves[0]["passed"]
    other = Signer.generate().public_key_pem
    with offline(monkeypatch, migrated_db):
        r_forged = verify_evidence_offline(forged, data["pub"])
        r_leaves = verify_evidence_offline(data["evidence"], data["pub"], json.dumps(leaves))
        r_key = verify_evidence_offline(data["evidence"], other)
        r_anchor_key = verify_anchor(data["anchor"], other)
    assert not r_forged.valid and not r_forged.signature_ok
    assert r_leaves.signature_ok and r_leaves.merkle_ok is False and not r_leaves.valid
    assert not r_key.valid and not r_key.signature_ok
    assert not r_anchor_key.valid and not r_anchor_key.signature_ok
