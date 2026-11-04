"""Security e2e tests that need database-level attacks or concurrency (QA-owned).

Concurrency, failure recovery, audit tampering (AT-21) and PII leakage (AT-24) on REAL
PostgreSQL. Every claim is checked with direct SQL.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

import pytest
import sqlalchemy as sa

pytestmark = pytest.mark.e2e


# ------------------------------------------------------------------ concurrency


def test_two_threads_executing_the_same_plan_commit_exactly_once(
    api_env: dict[str, Any], helpers: Any
) -> None:
    from lae.contracts.enums import ExecutionMode, ExecutionStatus
    from lae.errors import LaeError
    from lae.services import pipeline

    rt, truth = api_env["rt"], api_env["truth"]
    subject = helpers.subjects(truth)[0]
    others = sorted(k for k in truth.subjects if k != subject)
    flow = helpers.plan_for(subject)
    plan = flow["plan"]
    assert not plan["is_blocked"], plan["conflicts"]
    assert helpers.approve(plan).status_code == 200

    barrier = threading.Barrier(2)
    results: list[Any] = [None, None]

    def worker(i: int) -> None:
        barrier.wait()
        try:
            results[i] = pipeline.execute(
                rt, plan["id"], ExecutionMode.LIVE, f"qa-exec-{i}", auto_verify=False
            )
        except LaeError as exc:
            results[i] = exc
        except Exception as exc:  # anything else is a defect; keep it for the assertion
            results[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not any(t.is_alive() for t in threads), "an execution thread hung"

    ok = [r for r in results if getattr(r, "status", None) is ExecutionStatus.COMMITTED]
    errs = [r for r in results if isinstance(r, BaseException)]
    assert len(ok) == 1, results
    assert len(errs) == 1 and isinstance(errs[0], LaeError), repr(errs)
    assert errs[0].code in {
        "ALREADY_EXECUTED", "SERIALIZATION_CONFLICT", "ROW_COUNT_MISMATCH", "STALE_PLAN",
        "INVALID_STATE",
    }, errs[0].code  # fmt: skip
    committed = helpers.scalar(
        "SELECT count(*) FROM lae.execution_run WHERE plan_id = :p AND mode = 'LIVE' "
        "AND status = 'COMMITTED'",
        p=plan["id"],
    )
    assert committed == 1
    assert helpers.scalar("SELECT count(*) FROM app.customer WHERE id = :c", c=subject) == 0
    with api_env["engine"].connect() as c:
        remaining = {r[0] for r in c.execute(sa.text("SELECT id FROM app.customer"))}
    assert set(others) <= remaining, "an unrelated customer was deleted"
    chain = api_env["client"].post("/api/audit/verify-chain", headers=helpers.OPERATOR).json()
    with api_env["engine"].connect() as c:
        events = [
            tuple(r)
            for r in c.execute(
                sa.text(
                    "SELECT seq, event_type, left(prev_hash, 8), left(event_hash, 8) "
                    "FROM lae.audit_event ORDER BY seq"
                )
            )
        ]
    # concurrent appends must still form ONE valid hash chain
    assert chain["valid"], (chain, events[-12:])


# ------------------------------------------------------------------ failure recovery


def test_failed_execution_leaves_data_intact_and_plan_replannable(
    api_env: dict[str, Any], helpers: Any
) -> None:
    c, engine, truth = api_env["client"], api_env["engine"], api_env["truth"]
    subject = helpers.subjects(truth)[0]
    flow = helpers.plan_for(subject)
    assert helpers.approve(flow["plan"]).status_code == 200
    with engine.begin() as conn:  # drift after approval: the plan is now stale
        conn.execute(
            sa.text(
                "INSERT INTO app.customer_address (customer_id, line1, city, postal_code, country)"
                " VALUES (:c, 'drift street 1', 'Synthville', '00000', 'XX')"
            ),
            {"c": subject},
        )
    before = helpers.app_fingerprint()
    r = helpers.execute(flow["plan"]["id"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "STALE_PLAN", r.text
    assert helpers.app_fingerprint() == before, "a failed execution changed data"
    assert (
        helpers.scalar(
            "SELECT count(*) FROM lae.execution_run WHERE plan_id = :p AND status = 'COMMITTED'",
            p=flow["plan"]["id"],
        )
        == 0
    )

    # recovery: re-plan, approve, execute -> COMMITTED and the drifted row is erased too
    r = c.post(f"/api/erasure-plans/{flow['request_id']}/generate", headers=helpers.OPERATOR)
    assert r.status_code == 201, r.text
    new = r.json()
    assert new["id"] != flow["plan"]["id"]
    assert helpers.approve(new).status_code == 200
    r = helpers.execute(new["id"])
    assert r.status_code == 200 and r.json()["status"] == "COMMITTED", r.text
    assert helpers.scalar("SELECT count(*) FROM app.customer WHERE id = :c", c=subject) == 0
    assert (
        helpers.scalar(
            "SELECT count(*) FROM app.customer_address WHERE customer_id = :c", c=subject
        )
        == 0
    )


# ------------------------------------------------------------------ audit tampering (AT-21)


def _audit_triggers(engine: sa.Engine) -> list[str]:
    with engine.connect() as c:
        return [
            r[0]
            for r in c.execute(
                sa.text(
                    "SELECT tgname FROM pg_trigger WHERE tgrelid = 'lae.audit_event'::regclass "
                    "AND NOT tgisinternal ORDER BY tgname"
                )
            )
        ]


def test_audit_log_is_append_only_and_tampering_is_detected(
    api_env: dict[str, Any], helpers: Any
) -> None:
    c, engine = api_env["client"], api_env["engine"]
    helpers.plan_for(helpers.subjects(api_env["truth"])[0])  # produce several events
    seq = helpers.scalar("SELECT min(seq) + 1 FROM lae.audit_event")
    n_before = helpers.scalar("SELECT count(*) FROM lae.audit_event")
    assert seq is not None and n_before >= 3

    for stmt in (
        "UPDATE lae.audit_event SET actor = 'mallory' WHERE seq = :s",
        "UPDATE lae.audit_event SET payload_canonical = '{}' WHERE seq = :s",
        "DELETE FROM lae.audit_event WHERE seq = :s",
        "DELETE FROM lae.audit_event",
        "TRUNCATE lae.audit_event",
        "TRUNCATE lae.audit_event CASCADE",
    ):
        with engine.connect() as conn, pytest.raises(sa.exc.DBAPIError):
            conn.execute(sa.text(stmt), {"s": seq})
            conn.commit()
    assert helpers.scalar("SELECT count(*) FROM lae.audit_event") == n_before
    v = c.post("/api/audit/verify-chain", headers=helpers.OPERATOR).json()
    assert v["valid"] is True and v["first_invalid_seq"] is None

    # superuser tamper with the triggers disabled -> detected at exactly that seq
    triggers = _audit_triggers(engine)
    assert triggers, "append-only triggers are missing"
    original = helpers.scalar("SELECT payload_canonical FROM lae.audit_event WHERE seq = :s", s=seq)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("ALTER TABLE lae.audit_event DISABLE TRIGGER USER"))
            conn.execute(
                sa.text("UPDATE lae.audit_event SET payload_canonical = :p WHERE seq = :s"),
                {"p": json.dumps({"tampered": True}, separators=(",", ":")), "s": seq},
            )
            conn.execute(sa.text("ALTER TABLE lae.audit_event ENABLE TRIGGER USER"))
        v = c.post("/api/audit/verify-chain", headers=helpers.OPERATOR).json()
        assert v["valid"] is False
        assert v["first_invalid_seq"] == seq, v
    finally:  # restore the original row and the triggers whatever happened
        with engine.begin() as conn:
            conn.execute(sa.text("ALTER TABLE lae.audit_event DISABLE TRIGGER USER"))
            conn.execute(
                sa.text("UPDATE lae.audit_event SET payload_canonical = :p WHERE seq = :s"),
                {"p": original, "s": seq},
            )
            conn.execute(sa.text("ALTER TABLE lae.audit_event ENABLE TRIGGER USER"))
    assert _audit_triggers(engine) == triggers
    with engine.connect() as conn:
        enabled = conn.execute(
            sa.text(
                "SELECT bool_and(tgenabled = 'O') FROM pg_trigger "
                "WHERE tgrelid = 'lae.audit_event'::regclass AND NOT tgisinternal"
            )
        ).scalar_one()
    assert enabled, "append-only triggers were not re-enabled"
    assert c.post("/api/audit/verify-chain", headers=helpers.OPERATOR).json()["valid"] is True


# ------------------------------------------------------------------ no PII leakage (AT-24)


@pytest.mark.parametrize("fresh_db", [{"scale": "small", "seed": 42}], indirect=True)
def test_no_raw_pii_in_logs_responses_or_lae_schema_after_purge(
    api_env: dict[str, Any], helpers: Any, caplog: pytest.LogCaptureFixture
) -> None:
    from lae.db.engine import session_scope
    from lae.db.repositories import ArtifactRepo

    c, engine, truth, rt = api_env["client"], api_env["engine"], api_env["truth"], api_env["rt"]
    clean = helpers.subjects(truth, shared=False, hidden=False, referencing=None)
    assert clean, "the small/42 dataset must contain a subject without hidden copies"
    subject = clean[0]
    with engine.connect() as conn:
        email, phone, name = conn.execute(
            sa.text("SELECT email, phone, full_name FROM app.customer WHERE id = :c"),
            {"c": subject},
        ).one()
        lines = [
            r[0]
            for r in conn.execute(
                sa.text("SELECT line1 FROM app.customer_address WHERE customer_id = :c"),
                {"c": subject},
            )
        ]
    secrets_ = [s for s in (email, phone, name, *lines) if s]
    # the uninspectable external artifact caps the outcome at PARTIAL; deactivate it so the
    # flow reaches VERIFIED and the purge runs (restored in finally)
    with session_scope(rt.owner) as s:
        ext = ArtifactRepo(s).get_by_name("crm_export_files")
        assert ext is not None
        ArtifactRepo(s).deactivate(ext.definition.artifact_id)
    bodies: list[str] = []
    try:
        with caplog.at_level(logging.DEBUG):
            r = c.post("/api/erasure-requests", json={"subject_key": [subject]},
                       headers=helpers.OPERATOR)  # fmt: skip
            assert r.status_code == 201, r.text
            rid = r.json()["id"]
            bodies.append(r.text)
            bodies.append(c.post(f"/api/discovery/{rid}", headers=helpers.OPERATOR).text)
            p = c.post(f"/api/erasure-plans/{rid}/generate", headers=helpers.OPERATOR)
            bodies.append(p.text)
            plan = p.json()
            bodies.append(
                c.post(f"/api/erasure-plans/{plan['id']}/execute", json={"mode": "DRY_RUN"},
                       headers=helpers.OPERATOR).text
            )  # fmt: skip
            a = helpers.approve(plan)
            assert a.status_code == 200, a.text
            bodies.append(a.text)
            ex = helpers.execute(plan["id"])
            assert ex.status_code == 200, ex.text
            bodies.append(ex.text)
            for path in (
                f"/api/erasure-requests/{rid}",
                "/api/erasure-requests",
                f"/api/lineage/{rid}",
                f"/api/lineage/{rid}?level=COLUMN",
                "/api/lineage/schema?level=COLUMN",
                f"/api/erasure-plans/{plan['id']}",
                f"/api/verification/{rid}",
                f"/api/audit/{rid}",
                "/api/audit/events?limit=1000",
                f"/api/evidence/{rid}/download",
                f"/api/evidence/{rid}/leaves",
                f"/api/executions?request_id={rid}",
                "/api/stats",
                f"/api/customers?q={subject}",
            ):
                g = c.get(path, headers=helpers.OPERATOR)
                assert g.status_code == 200, (path, g.text[:200])
                bodies.append(g.text)
            v = c.get(f"/api/verification/{rid}", headers=helpers.OPERATOR).json()
            assert v["outcome"] == "VERIFIED", v
    finally:
        with session_scope(rt.owner) as s:
            ArtifactRepo(s).update(ext.definition, is_active=True)

    blob = ("\n".join(bodies) + "\n" + "\n".join(
        f"{r.getMessage()} {r.__dict__}" for r in caplog.records
    )).lower()  # fmt: skip
    for sval in secrets_:
        assert sval.lower() not in blob, "raw personal data in an API response or a log record"
    for body in bodies:  # no response field carries a raw subject key
        if body.startswith(("{", "[")):
            assert '"subject_key":' not in body and '"record_keys":' not in body

    # AT-24: after the purge, the lae schema holds no raw identifier and no raw key
    with engine.connect() as conn:
        tables = [
            r[0]
            for r in conn.execute(
                sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'lae' ORDER BY 1")
            )
        ]
        dump = "\n".join(
            row[0]
            for t in tables
            for row in conn.execute(sa.text(helpers.render("SELECT t::text FROM {} t", "lae", t)))
        ).lower()
        raw_left = conn.execute(
            sa.text(
                "SELECT (SELECT count(*) FROM lae.erasure_request WHERE id = :r "
                "AND subject_key IS NOT NULL) + (SELECT count(*) FROM lae.plan_item "
                "WHERE plan_id = :p AND record_keys IS NOT NULL) + (SELECT count(*) FROM "
                "lae.record_dependency WHERE plan_id = :p AND (parent_key IS NOT NULL OR "
                "child_key IS NOT NULL))"
            ),
            {"r": rid, "p": plan["id"]},
        ).scalar_one()
    for sval in secrets_:
        assert sval.lower() not in dump, "raw personal data stored in the lae schema"
    assert raw_left == 0, "raw subject/record keys survived the purge"
