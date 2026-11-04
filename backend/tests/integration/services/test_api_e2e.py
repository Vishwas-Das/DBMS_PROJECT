"""End-to-end through the HTTP API on REAL PostgreSQL (Lead-owned integration tests).

Every erasure claim is checked with direct SQL against the database, independently of the
engine's own verification. Run with LAE_TEST_DATABASE_URL pointing at a dedicated test DB.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from lae.api.app import create_app
from lae.audit.evidence import verify_evidence_offline
from lae.audit.signing import Signer
from lae.config import Settings
from lae.contracts.types import TableRef
from lae.db.engine import session_scope
from lae.db.repositories import ArtifactRepo, AuditRepo, RequestRepo
from lae.services.impl import LaeServices
from lae.services.runtime import Runtime

pytestmark = pytest.mark.integration
TOKEN = "e2e-token-" + "x" * 22
H = {"X-LAE-Token": TOKEN, "X-LAE-Actor": "e2e-operator"}
HA = {"X-LAE-Token": TOKEN, "X-LAE-Actor": "e2e-approver"}  # four-eyes: approver != executor


@pytest.fixture
def env(
    seeded_db: tuple[sa.Engine, Any], migrated_url: str, mark_dataset_dirty: Any, tmp_path: Path
) -> Iterator[dict[str, Any]]:
    """Fresh tiny dataset per test (tests commit erasures), own signing key, real PostgreSQL."""
    engine, truth = seeded_db
    with engine.begin() as conn:  # re-seeding resets `app` only; close earlier tests' requests
        conn.execute(
            sa.text(
                "UPDATE lae.erasure_request SET status = 'CANCELLED' "
                "WHERE status NOT IN ('VERIFIED', 'CANCELLED')"
            )
        )
    Signer.generate().save(tmp_path / "signing.pem")
    rt = Runtime.from_settings(
        Settings(
            _env_file=None,  # QA D3: never pick up the repo .env (it points at the dev DB)
            database_url=migrated_url,
            reader_url=migrated_url,
            api_token=None,
            hmac_key="h" * 32,
            signing_key_path=tmp_path / "signing.pem",
            target_schemas=["app"],
        )
    )
    rt.anchor_dir = tmp_path / "anchors"
    client = TestClient(create_app(lambda: LaeServices(rt), api_token=TOKEN), headers=H)
    try:
        yield {"engine": engine, "truth": truth, "rt": rt, "client": client}
    finally:
        rt.dispose()
        mark_dataset_dirty()


# ------------------------------------------------------------------ helpers


def _subjects(
    truth: Any, *, shared: bool = False, hidden: bool = False, referencing: bool | None = False
) -> list[int]:
    out = []
    for sid, v in sorted(truth.subjects.items()):
        if bool(v["shared"]) != shared or bool(v["hidden_copies"]) != hidden:
            continue
        if referencing is not None and bool(v["referencing_other"]) != referencing:
            continue
        out.append(sid)
    return out


def _exists(engine: sa.Engine, table: str, pk_cols: list[str], pk: list[Any]) -> bool:
    t = TableRef.parse(table)
    where = " AND ".join(f'"{c}"::text = :v{i}' for i, c in enumerate(pk_cols))
    q = sa.text(f'SELECT count(*) FROM "{t.schema}"."{t.name}" WHERE {where}')
    with engine.connect() as c:
        return bool(c.execute(q, {f"v{i}": str(v) for i, v in enumerate(pk)}).scalar())


PK = {
    "app.customer": ["id"],
    "app.customer_address": ["id"],
    "app.loyalty_account": ["customer_id"],
    "app.purchase_order": ["id"],
    "app.order_line": ["order_id", "line_no"],
    "app.payment": ["id"],
    "app.shipment": ["id"],
    "app.support_ticket": ["id"],
    "app.ticket_message": ["id"],
    "app.document": ["id"],
    "app.document_embedding": ["id"],
    "app.customer_profile_cache": ["customer_id"],
    "app.customer_segment": ["customer_id"],
    "app.customer_ltv": ["customer_id"],
}


def _table_counts(engine: sa.Engine) -> dict[str, int]:
    with engine.connect() as c:
        return {t: int(c.execute(sa.text(f"SELECT count(*) FROM {t}")).scalar_one()) for t in PK}


def _run_to_plan(client: TestClient, subject: int) -> dict[str, Any]:
    r = client.post("/api/erasure-requests", json={"subject_key": [subject]}, headers=H)
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    r = client.post(f"/api/discovery/{rid}", headers=H)
    assert r.status_code == 200, r.text
    r = client.post(f"/api/erasure-plans/{rid}/generate", headers=H)
    assert r.status_code == 201, r.text
    return {"request_id": rid, "plan": r.json(), "discovery": None}


# ------------------------------------------------------------------ tests


def test_health_and_stats_are_live(env: dict[str, Any]) -> None:
    c = env["client"]
    h = c.get("/api/health").json()
    assert h["database"] is True and h["pgvector"] and h["pgcrypto"]
    s = c.get("/api/stats").json()
    with env["engine"].connect() as conn:
        n = conn.execute(sa.text("SELECT count(*) FROM app.customer")).scalar_one()
    assert s["synthetic_customers"] == n  # not fabricated: equals a direct COUNT


def test_unauthorized_mutations_are_rejected(env: dict[str, Any]) -> None:
    c = TestClient(create_app(lambda: LaeServices(env["rt"]), api_token=TOKEN))  # no token
    before = _table_counts(env["engine"])
    assert c.get("/api/stats").status_code == 401  # reads are protected too
    assert c.get("/api/health").status_code == 200  # only health is public
    assert c.post("/api/erasure-requests", json={"subject_key": [1]}).status_code == 401
    assert (
        c.post(
            "/api/erasure-plans/00000000-0000-0000-0000-000000000000/execute",
            json={"mode": "LIVE"},
            headers={"X-LAE-Token": "nope"},
        ).status_code
        == 401
    )
    assert _table_counts(env["engine"]) == before


def test_full_erasure_happy_path_checked_against_the_database(env: dict[str, Any]) -> None:
    c, engine, truth = env["client"], env["engine"], env["truth"]
    subject = _subjects(truth)[0]
    gt = truth.subject(subject)
    others = [k for k in truth.subjects if k != subject]
    flow = _run_to_plan(c, subject)
    plan, rid = flow["plan"], flow["request_id"]
    assert not plan["is_blocked"], plan["conflicts"]

    # dry run must not change a single row
    before = _table_counts(engine)
    r = c.post(f"/api/erasure-plans/{plan['id']}/execute", json={"mode": "DRY_RUN"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ROLLED_BACK"
    assert _table_counts(engine) == before

    # approval is bound to the exact hash
    bad = c.post(
        f"/api/erasure-plans/{plan['id']}/approve", json={"plan_hash": "0" * 64}, headers=HA
    )
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "PLAN_HASH_MISMATCH"
    ok = c.post(
        f"/api/erasure-plans/{plan['id']}/approve",
        json={"plan_hash": plan["plan_hash"]},
        headers=HA,
    )
    assert ok.status_code == 200 and ok.json()["status"] == "APPROVED"

    r = c.post(f"/api/erasure-plans/{plan['id']}/execute", json={"mode": "LIVE"}, headers=H)
    assert r.status_code == 200, r.text
    ex = r.json()
    assert ex["status"] == "COMMITTED" and ex["verification"] is not None

    # ---- independent DB checks (not the engine's report)
    retained = {(t, tuple(k)) for t, k, _ in gt["retained"]}
    for table, key in gt["owned"]:
        present = _exists(engine, table, PK[table], key)
        if (table, tuple(key)) in retained:
            assert present, f"retained row {table} was destroyed"
        else:
            assert not present, f"owned row in {table} survived erasure"
    for table, key in gt["derived_rows"]:
        if table in PK:
            assert not _exists(engine, table, PK[table], key), f"derived row in {table} survived"
    with engine.connect() as conn:
        po = conn.execute(
            sa.text("SELECT count(*) FROM app.purchase_order WHERE customer_id = :c"),
            {"c": subject},
        ).scalar_one()
        remaining_customers = {r[0] for r in conn.execute(sa.text("SELECT id FROM app.customer"))}
    assert po == 0, "retained orders still reference the erased subject"
    assert set(others) <= remaining_customers, "an unrelated customer was deleted"
    assert subject not in remaining_customers

    # ---- verification result is honest: the external CRM export cannot be verified in-DB
    v = c.get(f"/api/verification/{rid}").json()
    assert v["outcome"] == "PARTIAL"
    assert v["verified_artifacts"] == v["required_artifacts"] - 1
    assert all(ch["passed"] for ch in v["checks"])

    # ---- evidence verifies OFFLINE with the exported chain
    bundle = c.get(f"/api/evidence/{rid}/download").text
    leaves = c.get(f"/api/evidence/{rid}/leaves").text
    with session_scope(env["rt"].reader, read_only=True) as s:
        chain = json.dumps(AuditRepo(s).export_chain())
    res = verify_evidence_offline(bundle, env["rt"].signer.public_key_pem, leaves, chain)
    assert res.valid and res.signature_ok and res.merkle_ok and res.chain_ok, res.errors
    audit = c.get(f"/api/audit/{rid}").json()
    assert audit["chain"]["valid"] and audit["evidence"]["signature_valid"]
    types = [e["event_type"] for e in audit["events"]]
    for t in [
        "REQUEST_RECEIVED",
        "PLAN_CREATED",
        "PLAN_APPROVED",
        "EXECUTION_STARTED",
        "EXECUTION_COMMITTED",
        "VERIFICATION_COMPLETED",
        "EVIDENCE_ISSUED",
    ]:
        assert t in types, t

    # ---- idempotency
    again = c.post(f"/api/erasure-plans/{plan['id']}/execute", json={"mode": "LIVE"}, headers=H)
    assert again.status_code == 409


def test_verified_outcome_purges_raw_subject_data(env: dict[str, Any]) -> None:
    """Without the uninspectable external artifact, a clean subject reaches VERIFIED + purge."""
    c, rt, truth = env["client"], env["rt"], env["truth"]
    with session_scope(rt.owner) as s:
        ext = ArtifactRepo(s).get_by_name("crm_export_files")
        assert ext is not None
        ArtifactRepo(s).deactivate(ext.definition.artifact_id)
    try:
        subject = _subjects(truth)[1]
        flow = _run_to_plan(c, subject)
        plan = flow["plan"]
        c.post(
            f"/api/erasure-plans/{plan['id']}/approve",
            json={"plan_hash": plan["plan_hash"]},
            headers=HA,
        )
        ex = c.post(f"/api/erasure-plans/{plan['id']}/execute", json={"mode": "LIVE"}, headers=H)
        assert ex.status_code == 200, ex.text
        v = c.get(f"/api/verification/{flow['request_id']}").json()
        assert v["outcome"] == "VERIFIED" and v["verified_artifacts"] == v["required_artifacts"]
        with session_scope(rt.reader, read_only=True) as s:
            assert RequestRepo(s).subject_key(flow["request_id"]) is None  # raw key purged
            raw = s.execute(
                sa.text(
                    "SELECT count(*) FROM lae.plan_item "
                    "WHERE plan_id = :p AND record_keys IS NOT NULL"
                ),
                {"p": plan["id"]},
            ).scalar_one()
        assert raw == 0
        assert c.get(f"/api/erasure-requests/{flow['request_id']}").json()["status"] == "VERIFIED"
    finally:
        with session_scope(rt.owner) as s:
            ArtifactRepo(s).update(ext.definition, is_active=True)


def test_hidden_copies_fail_verification_and_are_suggested(env: dict[str, Any]) -> None:
    c, engine, truth = env["client"], env["engine"], env["truth"]
    subject = _subjects(truth, hidden=True, referencing=None)[0]
    flow = _run_to_plan(c, subject)
    plan = flow["plan"]
    if plan["is_blocked"]:
        pytest.skip("first hidden-copy subject has a blocking conflict at this seed")
    c.post(
        f"/api/erasure-plans/{plan['id']}/approve",
        json={"plan_hash": plan["plan_hash"]},
        headers=HA,
    )
    c.post(f"/api/erasure-plans/{plan['id']}/execute", json={"mode": "LIVE"}, headers=H)
    v = c.get(f"/api/verification/{flow['request_id']}").json()
    assert v["outcome"] == "FAILED"
    hits = {(h["table"], h["column"]): h["count"] for h in v["residual_hits"]}
    assert ("app.marketing_event_log", "email") in hits
    with engine.connect() as conn:  # the copies really are still there
        assert conn.execute(sa.text("SELECT count(*) FROM app.marketing_event_log")).scalar_one()
    graph = c.get("/api/lineage/schema").json()
    suggested = [
        e
        for e in graph["edges"]
        if e["status"] == "SUGGESTED"
        and e["target"].endswith("marketing_event_log")
        and e["evidence"].get("evidence") == "residual_scan"
    ]
    assert suggested, "residual hits were not fed back as suggested lineage"


def test_drift_between_approval_and_execution_is_detected(env: dict[str, Any]) -> None:
    c, engine, truth = env["client"], env["engine"], env["truth"]
    subject = _subjects(truth, referencing=None)[0]
    flow = _run_to_plan(c, subject)
    plan = flow["plan"]
    c.post(
        f"/api/erasure-plans/{plan['id']}/approve",
        json={"plan_hash": plan["plan_hash"]},
        headers=HA,
    )
    with engine.begin() as conn:  # concurrent business activity after approval
        conn.execute(
            sa.text(
                "INSERT INTO app.customer_address (customer_id, line1, city, postal_code, country) "
                "VALUES (:c, 'late street 1', 'Synthville', '00000', 'XX')"
            ),
            {"c": subject},
        )
    before = _table_counts(engine)
    r = c.post(f"/api/erasure-plans/{plan['id']}/execute", json={"mode": "LIVE"}, headers=H)
    assert r.status_code == 409 and r.json()["error"]["code"] == "STALE_PLAN", r.text
    assert _table_counts(engine) == before  # nothing changed
    assert _exists(engine, "app.customer", ["id"], [subject])


def test_shared_records_block_live_execution(env: dict[str, Any]) -> None:
    c, truth = env["client"], env["truth"]
    shared = _subjects(truth, shared=True, hidden=False, referencing=None) or _subjects(
        truth, shared=True, hidden=True, referencing=None
    )
    assert shared, "the generator must create subjects with shared records"
    flow = _run_to_plan(c, shared[0])
    plan = flow["plan"]
    codes = {x["code"] for x in plan["conflicts"]}
    assert "SHARED_RECORD" in codes
    if plan["is_blocked"]:
        r = c.post(
            f"/api/erasure-plans/{plan['id']}/approve",
            json={"plan_hash": plan["plan_hash"]},
            headers=HA,
        )
        assert r.status_code == 409


def test_no_raw_pii_in_responses_or_logs(
    env: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    c, engine, truth = env["client"], env["engine"], env["truth"]
    subject = _subjects(truth, referencing=None)[-1]
    with engine.connect() as conn:
        email, phone = conn.execute(
            sa.text("SELECT email, phone FROM app.customer WHERE id = :c"), {"c": subject}
        ).one()
    bodies: list[str] = []
    with caplog.at_level(logging.DEBUG):
        r = c.post("/api/erasure-requests", json={"subject_key": [subject]}, headers=H)
        rid = r.json()["id"]
        bodies.append(r.text)
        for path in [f"/api/discovery/{rid}"]:
            bodies.append(c.post(path, headers=H).text)
        p = c.post(f"/api/erasure-plans/{rid}/generate", headers=H)
        bodies.append(p.text)
        bodies.append(c.get(f"/api/lineage/{rid}").text)
        bodies.append(c.get(f"/api/audit/{rid}").text)
        bodies.append(c.get("/api/audit/events?limit=1000").text)
    blob = "\n".join(bodies) + "\n".join(r.getMessage() for r in caplog.records)
    assert email not in blob and email.lower() not in blob.lower()
    if phone:
        assert phone not in blob
    with engine.connect() as conn:  # the audit store holds no raw identifier either
        dump = "\n".join(
            r[0] for r in conn.execute(sa.text("SELECT payload_canonical FROM lae.audit_event"))
        )
    assert email.lower() not in dump.lower()


def test_audit_events_cannot_be_modified(env: dict[str, Any]) -> None:
    c = env["client"]
    c.post("/api/erasure-requests", json={"subject_key": [_subjects(env["truth"])[0]]}, headers=H)
    with env["engine"].connect() as conn:
        seq = conn.execute(sa.text("SELECT max(seq) FROM lae.audit_event")).scalar_one()
    assert seq is not None, "need at least one audit event to attack"
    # a row-level trigger only fires for rows that match, so target an existing event
    with env["engine"].connect() as conn, pytest.raises(sa.exc.DBAPIError):
        conn.execute(
            sa.text("UPDATE lae.audit_event SET actor = 'mallory' WHERE seq = :s"), {"s": seq}
        )
    with env["engine"].connect() as conn, pytest.raises(sa.exc.DBAPIError):
        conn.execute(sa.text("DELETE FROM lae.audit_event"))
    assert env["client"].post("/api/audit/verify-chain").json()["valid"] is True
