"""Regression tests for the independent security review (2026-11-04), on REAL PostgreSQL.

H2 auth on reads · H3 purge across plan versions · M1 artifact-SQL breakout · M2 non-superuser
lae_app role · M4 canonical subject key · M5 re-plan after STALE_PLAN · M7 four-eyes · L4 token.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from lae.api.app import create_app
from lae.audit.signing import Signer
from lae.config import Settings
from lae.contracts.types import TableRef
from lae.db.engine import session_scope
from lae.db.target import PgTargetReader
from lae.errors import ValidationError
from lae.services.impl import LaeServices
from lae.services.runtime import Runtime, derive_subkey

pytestmark = pytest.mark.integration
TOKEN = "sec-token-" + "y" * 22
OP = {"X-LAE-Token": TOKEN, "X-LAE-Actor": "operator"}
AP = {"X-LAE-Token": TOKEN, "X-LAE-Actor": "approver"}


@pytest.fixture
def env(
    seeded_db: tuple[sa.Engine, Any], migrated_url: str, mark_dataset_dirty: Any, tmp_path: Path
) -> Iterator[dict[str, Any]]:
    engine, truth = seeded_db
    with engine.begin() as conn:  # re-seeding resets `app` only; close earlier tests' requests
        conn.execute(
            sa.text(
                "UPDATE lae.erasure_request SET status = 'CANCELLED' "
                "WHERE status NOT IN ('VERIFIED', 'CANCELLED')"
            )
        )
    Signer.generate().save(tmp_path / "k.pem")
    rt = Runtime.from_settings(
        Settings(
            _env_file=None,  # QA D3: never pick up the repo .env (it points at the dev DB)
            database_url=migrated_url,
            reader_url=migrated_url,
            api_token=None,
            hmac_key="s" * 32,
            signing_key_path=tmp_path / "k.pem",
            target_schemas=["app"],
        )
    )
    rt.anchor_dir = tmp_path / "anchors"
    client = TestClient(create_app(lambda: LaeServices(rt), api_token=TOKEN), headers=OP)
    try:
        yield {"engine": engine, "truth": truth, "rt": rt, "client": client}
    finally:
        rt.dispose()
        mark_dataset_dirty()


def _clean_subject(truth: Any, i: int = 0) -> int:
    return [
        k for k, v in sorted(truth.subjects.items()) if not v["shared"] and not v["hidden_copies"]
    ][i]


def _plan(c: TestClient, subject: int | str) -> tuple[str, dict[str, Any]]:
    r = c.post("/api/erasure-requests", json={"subject_key": [subject]})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    p = c.post(f"/api/erasure-plans/{rid}/generate")
    assert p.status_code == 201, p.text
    return rid, p.json()


def test_reads_require_token_and_host_is_checked(env: dict[str, Any]) -> None:
    app = create_app(lambda: LaeServices(env["rt"]), api_token=TOKEN)
    anon = TestClient(app)
    for path in [
        "/api/stats",
        "/api/customers?q=a",
        "/api/erasure-requests",
        "/api/audit/events",
        "/api/benchmarks",
    ]:
        assert anon.get(path).status_code == 401, path
    assert anon.get("/api/health").status_code == 200
    # non-ASCII token must be a clean 401, not a 500 (L4)
    assert (
        anon.get("/api/stats", headers={"X-LAE-Token": "tökén".encode("latin-1")}).status_code
        == 401
    )
    # DNS-rebinding style Host header is refused
    evil = TestClient(app, base_url="http://attacker.example")
    assert evil.get("/api/health").status_code == 400


def test_like_wildcards_do_not_enumerate_customers(env: dict[str, Any]) -> None:
    c = env["client"]
    assert c.get("/api/customers", params={"q": "_"}).json() == []
    assert c.get("/api/customers", params={"q": "%"}).json() == []


def test_four_eyes_and_replan_after_stale(env: dict[str, Any]) -> None:
    c, engine = env["client"], env["engine"]
    subject = _clean_subject(env["truth"])
    rid, plan = _plan(c, subject)
    # approver == executor is refused
    same = {"X-LAE-Token": TOKEN, "X-LAE-Actor": "operator"}
    assert (
        c.post(
            f"/api/erasure-plans/{plan['id']}/approve",
            json={"plan_hash": plan["plan_hash"]},
            headers=same,
        ).status_code
        == 200
    )
    r = c.post(f"/api/erasure-plans/{plan['id']}/execute", json={"mode": "LIVE"}, headers=same)
    assert r.status_code == 409 and "separation of duties" in r.json()["error"]["message"]
    # M5: drift after approval -> STALE_PLAN, and the request can be RE-PLANNED (was stuck)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO app.customer_address (customer_id, line1, city, postal_code, country) "
                "VALUES (:c, 'x', 'y', 'z', 'XX')"
            ),
            {"c": subject},
        )
    other = {"X-LAE-Token": TOKEN, "X-LAE-Actor": "executor-2"}
    r = c.post(f"/api/erasure-plans/{plan['id']}/execute", json={"mode": "LIVE"}, headers=other)
    assert r.status_code == 409 and r.json()["error"]["code"] == "STALE_PLAN", r.text
    p2 = c.post(f"/api/erasure-plans/{rid}/generate")
    assert p2.status_code == 201, p2.text
    plan2 = p2.json()
    assert plan2["version"] == 2
    assert c.get(f"/api/erasure-plans/{plan['id']}").json()["status"] == "SUPERSEDED"
    c.post(
        f"/api/erasure-plans/{plan2['id']}/approve",
        json={"plan_hash": plan2["plan_hash"]},
        headers=AP,
    )
    ex = c.post(f"/api/erasure-plans/{plan2['id']}/execute", json={"mode": "LIVE"})
    assert ex.status_code == 200 and ex.json()["status"] == "COMMITTED", ex.text
    with engine.connect() as conn:  # the late address was erased too
        assert (
            conn.execute(
                sa.text("SELECT count(*) FROM app.customer_address WHERE customer_id = :c"),
                {"c": subject},
            ).scalar_one()
            == 0
        )
    # H3: the superseded version no longer holds raw keys
    with engine.connect() as conn:
        raw = conn.execute(
            sa.text(
                "SELECT count(*) FROM lae.plan_item WHERE plan_id = :p AND record_keys IS NOT NULL"
            ),
            {"p": plan["id"]},
        ).scalar_one()
        raw_deps = conn.execute(
            sa.text(
                "SELECT count(*) FROM lae.record_dependency WHERE plan_id = :p "
                "AND (parent_key IS NOT NULL OR child_key IS NOT NULL)"
            ),
            {"p": plan["id"]},
        ).scalar_one()
    assert raw == 0 and raw_deps == 0


def test_subject_key_is_canonical_and_duplicates_rejected(env: dict[str, Any]) -> None:
    c = env["client"]
    subject = _clean_subject(env["truth"])
    a = c.post("/api/erasure-requests", json={"subject_key": [subject]})
    assert a.status_code == 201
    # the same subject spelled as a padded string is the same subject -> duplicate refused
    b = c.post("/api/erasure-requests", json={"subject_key": [f"0{subject}"]})
    assert b.status_code == 409, b.text
    other = _clean_subject(env["truth"], 1)
    s1 = c.post("/api/erasure-requests", json={"subject_key": [f"00{other}"]}).json()
    with session_scope(env["rt"].reader, read_only=True) as s:
        from lae.db.repositories import RequestRepo

        assert RequestRepo(s).subject_key(s1["id"]) == (other,)  # stored canonically as int


def test_artifact_sql_breakout_is_rejected(env: dict[str, Any]) -> None:
    engine, rt = env["engine"], env["rt"]
    c = env["client"]
    _plan(c, _clean_subject(env["truth"]))  # ensures a snapshot exists
    evil = "SELECT 1) x; COMMIT; DELETE FROM app.customer; SELECT (1"
    with engine.connect() as conn:
        before = conn.execute(sa.text("SELECT count(*) FROM app.customer")).scalar_one()
    from lae.services import pipeline as P  # noqa: N812

    sid = P.latest_snapshot(rt)
    with session_scope(rt.owner) as s:
        from lae.db.repositories import SnapshotRepo

        reader = PgTargetReader(s, SnapshotRepo(s).load_catalog(sid), allowed_sql=[evil])
        with pytest.raises(ValidationError):
            reader.symmetric_difference_count(TableRef("app", "customer_ltv"), evil)
        with pytest.raises(ValidationError):
            reader.run_readonly_sql("SELECT 1; DELETE FROM app.customer", {})
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM app.customer")).scalar_one() == before


def test_app_role_cannot_tamper_with_audit(env: dict[str, Any]) -> None:
    """M2: the non-superuser lae_app role owns `app` objects but cannot touch the audit chain."""
    engine = env["engine"]
    env["client"].post(
        "/api/erasure-requests", json={"subject_key": [_clean_subject(env["truth"])]}
    )
    with engine.begin() as conn:
        conn.execute(sa.text("SELECT lae.grant_app_role()"))
    with engine.connect() as conn:
        is_super = conn.execute(
            sa.text("SELECT rolsuper FROM pg_roles WHERE rolname = 'lae_app'")
        ).scalar_one()
        assert is_super is False
        conn.execute(sa.text("SET ROLE lae_app"))
        conn.execute(sa.text("REFRESH MATERIALIZED VIEW app.customer_ltv"))  # owner: allowed
        for stmt in [
            "UPDATE lae.audit_event SET actor = 'x'",
            "DELETE FROM lae.audit_event",
            "TRUNCATE lae.audit_event",
            "ALTER TABLE lae.audit_event DISABLE TRIGGER trg_audit_event_append_only",
        ]:
            conn.execute(sa.text("SAVEPOINT sp"))
            with pytest.raises(sa.exc.DBAPIError) as ei:
                conn.execute(sa.text(stmt))
            assert (
                "permission denied" in str(ei.value).lower()
                or "must be owner" in str(ei.value).lower()
            ), stmt
            conn.execute(sa.text("ROLLBACK TO SAVEPOINT sp"))
        conn.rollback()


def test_hmac_subkeys_are_domain_separated(env: dict[str, Any]) -> None:
    rt = env["rt"]
    master = b"s" * 32
    keys = {
        derive_subkey(master, p)
        for p in ["lae/audit-identity/v1", "lae/residual-probe/v1", "lae/pseudonym/v1"]
    }
    assert len(keys) == 3 and master not in keys
    assert rt.probe_key != rt.pseudonym_key
    assert rt.keyring.digest("42") != rt.probe_keyring.digest("42")


def test_schema_graph_get_never_creates_a_snapshot(env: dict[str, Any]) -> None:
    engine, c = env["engine"], env["client"]
    with engine.begin() as conn:
        n0 = conn.execute(sa.text("SELECT count(*) FROM lae.lineage_snapshot")).scalar_one()
    r = c.get("/api/lineage/schema")
    with engine.connect() as conn:
        n1 = conn.execute(sa.text("SELECT count(*) FROM lae.lineage_snapshot")).scalar_one()
    assert n1 == n0  # a GET must not write
    assert r.status_code in (200, 404)
    if n0 == 0:
        assert r.status_code == 404


def test_shared_name_does_not_cause_false_residual_failure(env: dict[str, Any]) -> None:
    """Frontend live run: a different customer with the same name made a correct erasure FAIL.
    Ambiguous (non-unique) probes are now excluded at planning time; unique ones still work."""
    c, engine, rt, truth = env["client"], env["engine"], env["rt"], env["truth"]
    from lae.db.repositories import ArtifactRepo

    with session_scope(rt.owner) as s:
        ext = ArtifactRepo(s).get_by_name("crm_export_files")
        assert ext is not None
        ArtifactRepo(s).deactivate(ext.definition.artifact_id)
    try:
        subject = _clean_subject(truth)
        other = next(k for k in sorted(truth.subjects) if k != subject)
        with engine.begin() as conn:  # a namesake: same full_name, different person
            conn.execute(
                sa.text(
                    "UPDATE app.customer SET full_name = (SELECT full_name FROM app.customer "
                    "WHERE id = :s) WHERE id = :o"
                ),
                {"s": subject, "o": other},
            )
        rid, plan = _plan(c, subject)
        c.post(
            f"/api/erasure-plans/{plan['id']}/approve",
            json={"plan_hash": plan["plan_hash"]},
            headers=AP,
        )
        ex = c.post(f"/api/erasure-plans/{plan['id']}/execute", json={"mode": "LIVE"})
        assert ex.status_code == 200, ex.text
        v = c.get(f"/api/verification/{rid}").json()
        assert v["residual_hits"] == [], v["residual_hits"]
        assert v["outcome"] == "VERIFIED"
        events = c.get(f"/api/audit/{rid}").json()["events"]
        created = next(e for e in events if e["event_type"] == "PLAN_CREATED")
        assert created["payload"]["ambiguous_probes_excluded"] >= 1
        with engine.connect() as conn:  # the namesake is untouched
            assert (
                conn.execute(
                    sa.text("SELECT count(*) FROM app.customer WHERE id = :o"), {"o": other}
                ).scalar_one()
                == 1
            )
    finally:
        with session_scope(rt.owner) as s:
            ArtifactRepo(s).update(ext.definition, is_active=True)


def test_schema_change_creates_a_second_snapshot_and_keeps_decisions(env: dict[str, Any]) -> None:
    """Repository audit: edge ids were global, so a second snapshot hit the lineage_edge PK."""
    from lae.contracts.enums import EdgeStatus
    from lae.db.repositories import SnapshotRepo
    from lae.services import pipeline

    engine, rt, c = env["engine"], env["rt"], env["client"]

    def load(sid: Any) -> dict[Any, Any]:
        with session_scope(rt.owner) as s:
            return {
                (
                    e.origin,
                    e.level,
                    e.source,
                    e.source_columns,
                    e.target,
                    e.target_columns,
                    e.constraint_name,
                ): e
                for e in SnapshotRepo(s).load_edges(sid)
            }

    first = pipeline.latest_or_new_snapshot(rt, "t")
    inferred = [e for e in load(first).values() if e.origin.value == "INFERRED"]
    if inferred:
        pipeline.set_edge_status(rt, inferred[0].edge_id, EdgeStatus.REJECTED, "t")
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE app.product ADD COLUMN audit_note text"))
    try:
        _, plan = _plan(c, _clean_subject(env["truth"]))
        second = pipeline.latest_or_new_snapshot(rt, "t")
        third = pipeline.create_snapshot(rt, "t")  # explicit re-scan of an unchanged schema
        assert len({first, second, third}) == 3 and plan["items"]
        a, b, d = load(first), load(second), load(third)
        assert set(a) == set(b) == set(d)
        assert not {e.edge_id for e in a.values()} & {e.edge_id for e in b.values()}
        assert {k: e.status for k, e in a.items()} == {k: e.status for k, e in d.items()}
    finally:
        with engine.begin() as conn:
            conn.execute(sa.text("ALTER TABLE app.product DROP COLUMN audit_note"))


def test_plan_superseded_between_checks_and_execution_changes_nothing(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repository audit: status/approval are re-read inside the SERIALIZABLE transaction."""
    from lae.contracts.enums import ExecutionMode, PlanStatus
    from lae.db.repositories import PlanRepo
    from lae.errors import InvalidStateTransition
    from lae.services import pipeline

    c, engine, rt = env["client"], env["engine"], env["rt"]
    subject = _clean_subject(env["truth"])
    _, plan = _plan(c, subject)
    r = c.post(
        f"/api/erasure-plans/{plan['id']}/approve",
        json={"plan_hash": plan["plan_hash"]},
        headers={"X-LAE-Token": TOKEN, "X-LAE-Actor": "approver-9"},
    )
    assert r.status_code == 200, r.text
    # the supersede commits after the pre-checks, before the execution snapshot is taken
    start = pipeline.ExecutionRepo.start

    def start_then_supersede(self: Any, *a: Any, **k: Any) -> Any:
        out = start(self, *a, **k)
        PlanRepo(self.session).set_status(plan["id"], PlanStatus.SUPERSEDED)
        return out

    monkeypatch.setattr(pipeline.ExecutionRepo, "start", start_then_supersede)
    with pytest.raises(InvalidStateTransition):
        pipeline.execute(rt, plan["id"], ExecutionMode.LIVE, "executor-9")
    with engine.connect() as conn:
        n = conn.execute(
            sa.text("SELECT count(*) FROM app.customer WHERE id = :s"), {"s": subject}
        ).scalar_one()
    assert n == 1
