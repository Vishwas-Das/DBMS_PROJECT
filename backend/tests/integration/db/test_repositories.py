"""Repositories: ORM <-> contract round trips (rolled back)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.orm import Session

from lae.contracts.enums import (
    ArtifactKind,
    CheckType,
    ConflictCode,
    EdgeOrigin,
    EdgeStatus,
    ErasureStrategy,
    ExecutionMode,
    ExecutionStatus,
    LineageLevel,
    Normalization,
    OnDeleteAction,
    PiiCategory,
    PiiSource,
    PlanAction,
    PlanStatus,
    RecordRole,
    RefreshStrategy,
    RequestStatus,
    RetentionAction,
    Severity,
    SnapshotStatus,
    StepStatus,
    Traceability,
    Transform,
    VerificationOutcome,
    VerificationStatus,
)
from lae.contracts.types import (
    ArtifactDefinition,
    CatalogSnapshot,
    CheckResult,
    ColumnRef,
    ErasurePlan,
    ErrorInfo,
    KeyMapping,
    PiiColumn,
    PlanConflict,
    PlanItem,
    RelationshipEdge,
    ResidualProbe,
    StepResult,
    TableRef,
    VerificationReport,
)
from lae.db.catalog_queries import PgCatalogReader
from lae.db.records import RecordDependencyRow
from lae.db.repositories import (
    ArtifactRepo,
    AuditRepo,
    BenchmarkRepo,
    EvidenceRepo,
    ExecutionRepo,
    PlanRepo,
    PolicyRepo,
    RequestRepo,
    SigningKeyRepo,
    SnapshotRepo,
    VerificationRepo,
)
from lae.errors import (
    AlreadyExecutedError,
    InvalidStateTransition,
    NotFoundError,
    PlanHashMismatch,
    ValidationError,
)

pytestmark = pytest.mark.integration

H1, H2, H3 = "1" * 64, "2" * 64, "3" * 64
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def tref(n: str) -> TableRef:
    return TableRef("app", n)


@pytest.fixture
def session(seeded_db: tuple[Engine, Any], app_conn: Connection) -> Session:
    return Session(bind=app_conn, join_transaction_mode="create_savepoint")


@pytest.fixture
def catalog(app_conn: Connection) -> CatalogSnapshot:
    tables = PgCatalogReader(app_conn).list_tables(["app"])
    return CatalogSnapshot({t.ref: t for t in tables}, ("app",), H1)


@pytest.fixture
def snapshot(session: Session, catalog: CatalogSnapshot) -> UUID:
    repo = SnapshotRepo(session)
    snap = repo.create(["app"], H1)
    repo.save_catalog(snap.id, catalog)
    return snap.id


def fk_edge(src: str, tgt: str, scol: str, tcol: str, on: OnDeleteAction) -> RelationshipEdge:
    return RelationshipEdge(
        edge_id=uuid4(),
        level=LineageLevel.TABLE,
        origin=EdgeOrigin.DECLARED_FK,
        source=tref(src),
        target=tref(tgt),
        source_columns=(scol,),
        target_columns=(tcol,),
        transform=Transform.FOREIGN_KEY,
        traceability=Traceability.KEY_PRESERVING,
        status=EdgeStatus.CONFIRMED,
        on_delete=on,
        constraint_name=f"{tgt}_fk",
        details={"note": "fk"},
    )


def test_snapshot_catalog_and_edges_round_trip(
    session: Session, catalog: CatalogSnapshot, snapshot: UUID
) -> None:
    repo = SnapshotRepo(session)
    loaded = repo.load_catalog(snapshot)
    assert dict(loaded.tables) == dict(catalog.tables)
    assert loaded.fingerprint == H1 and loaded.schemas == ("app",)
    assert set(repo.table_ids(snapshot)) == set(catalog.tables)
    e1 = fk_edge("customer", "purchase_order", "id", "customer_id", OnDeleteAction.RESTRICT)
    e2 = RelationshipEdge(
        edge_id=uuid4(),
        level=LineageLevel.COLUMN,
        origin=EdgeOrigin.INFERRED,
        source=tref("customer"),
        target=tref("marketing_event_log"),
        source_columns=("email",),
        target_columns=("email",),
        transform=Transform.IDENTITY,
        traceability=Traceability.KEY_PRESERVING,
        status=EdgeStatus.SUGGESTED,
        confidence=0.966,
    )
    assert repo.save_edges(snapshot, [e1, e2]) == 2
    edges = repo.load_edges(snapshot)
    assert set(edges) == {e1, e2}
    assert repo.load_edges(snapshot, statuses=[EdgeStatus.CONFIRMED]) == [e1]
    assert repo.load_edges(snapshot, level=LineageLevel.COLUMN) == [e2]
    assert repo.get_edge(e1.edge_id).details == {"note": "fk"}
    confirmed = repo.set_edge_status(e2.edge_id, EdgeStatus.CONFIRMED)
    assert confirmed.status is EdgeStatus.CONFIRMED and confirmed.confidence == 0.966
    snap = repo.set_status(
        snapshot, SnapshotStatus.READY, table_count=17, edge_count=2, warnings=["w1"]
    )
    assert (snap.status, snap.table_count, snap.warnings) == (SnapshotStatus.READY, 17, ("w1",))
    assert repo.latest() == snap
    assert repo.get(snapshot) == snap
    assert snap in repo.list_snapshots()
    with pytest.raises(ValidationError):
        repo.save_edges(snapshot, [fk_edge("customer", "ghost", "id", "x", OnDeleteAction.CASCADE)])
    with pytest.raises(NotFoundError):
        repo.get(uuid4())
    with pytest.raises(NotFoundError):
        repo.get_edge(uuid4())


def test_artifacts(session: Session) -> None:
    repo = ArtifactRepo(session)
    seeded = {r.definition.name for r in repo.list_artifacts()}
    assert {"customer_ltv", "crm_export_files", "document_embedding"} <= seeded
    emb = repo.get_by_name("document_embedding")
    assert emb is not None
    assert emb.definition.key_mappings == (
        KeyMapping(tref("document"), ("id",), ("source_document_id",)),
    )
    ext = repo.get_by_name("crm_export_files")
    assert ext is not None and not ext.is_inspectable
    d = ArtifactDefinition(
        artifact_id=uuid4(),
        name="ticket_summary",
        kind=ArtifactKind.VIEW,
        target=tref("ticket_summary"),
        definition_sql="SELECT 1",
        refresh_strategy=RefreshStrategy.NONE,
    )
    rec = repo.add(d, is_auto_registered=True)
    assert rec.definition == d and rec.is_auto_registered and rec.is_inspectable
    upd = repo.update(
        ArtifactDefinition(d.artifact_id, d.name, d.kind, d.target, "SELECT 2", d.refresh_strategy)
    )
    assert upd.definition.definition_sql == "SELECT 2"
    assert repo.find_by_target(tref("ticket_summary")) == upd
    assert repo.deactivate(d.artifact_id).is_active is False
    assert d.artifact_id not in {a.artifact_id for a in repo.active_definitions()}
    assert repo.get(d.artifact_id).id == d.artifact_id
    assert repo.get_by_name("nope") is None and repo.find_by_target(tref("nope")) is None
    assert len(repo.list_artifacts(include_inactive=True)) == len(seeded) + 1


def test_policies_and_subject_roots(session: Session) -> None:
    repo = PolicyRepo(session)
    ps = repo.policy_set()
    assert ps.action_for(tref("purchase_order")) is RetentionAction.ANONYMIZE
    assert ps.action_for(tref("order_line")) is RetentionAction.RETAIN
    assert ps.action_for(tref("customer")) is RetentionAction.DELETE
    email = ColumnRef(tref("customer"), "email")
    assert ps.pii[email].use_for_residual_scan and ps.pii[email].is_confirmed
    r = repo.upsert_retention(tref("customer_address"), RetentionAction.RETAIN, "audit (synthetic)")
    assert (
        repo.upsert_retention(tref("customer_address"), RetentionAction.ANONYMIZE, "x").id == r.id
    )
    assert repo.delete_retention(tref("customer_address")) and not repo.delete_retention(
        tref("nope")
    )
    pii = PiiColumn(
        ColumnRef(tref("marketing_event_log"), "email"),
        PiiCategory.DIRECT_IDENTIFIER,
        ErasureStrategy.NULLIFY,
        is_confirmed=False,
    )
    rec = repo.upsert_pii(pii, PiiSource.HEURISTIC)
    assert rec.pii == pii and rec.source is PiiSource.HEURISTIC
    upd = repo.update_pii(
        rec.id,
        is_confirmed=True,
        use_for_residual_scan=True,
        strategy=ErasureStrategy.PSEUDONYMIZE,
        category=PiiCategory.SENSITIVE,
    )
    assert upd.pii.is_confirmed and upd.pii.strategy is ErasureStrategy.PSEUDONYMIZE
    assert repo.get_pii(rec.id) == upd
    assert rec.id not in {
        p.id for p in repo.list_pii(confirmed_only=False) if not p.pii.is_confirmed
    }
    root = repo.subject_root_by_name("customer")
    assert root is not None
    assert root.root.key_columns == ("id",) and root.root.identifier_columns == ("email", "phone")
    other = repo.add_subject_root("employee", tref("customer"), ["id"])
    assert repo.get_subject_root(other.id) == other
    assert [r.name for r in repo.list_subject_roots()] == ["customer", "employee"]
    assert repo.subject_root_by_name("nobody") is None


def _request(session: Session) -> Any:
    root = PolicyRepo(session).subject_root_by_name("customer")
    assert root is not None
    return RequestRepo(session).create(root.id, (7,), H1, "dpo", "test")


def test_requests_and_probes(session: Session) -> None:
    repo = RequestRepo(session)
    req = _request(session)
    assert req.status is RequestStatus.RECEIVED and req.has_subject_key
    assert repo.subject_key(req.id) == (7,)
    assert repo.get(req.id) == req
    assert req in repo.list_requests() and req in repo.find_by_hmac(req.subject_root_id, H1)
    assert repo.list_requests(status=RequestStatus.VERIFIED) == []
    probes = [
        ResidualProbe(ColumnRef(tref("customer"), "email"), Normalization.LOWER_TRIM, H2),
        ResidualProbe(ColumnRef(tref("customer"), "phone"), Normalization.DIGITS_ONLY, H3),
    ]
    assert repo.add_probes(req.id, probes + probes) == 2
    assert repo.add_probes(req.id, probes) == 0
    assert repo.add_probes(req.id, []) == 0
    assert repo.list_probes(req.id) == sorted(
        probes, key=lambda p: (p.origin, p.normalization.value, p.value_hmac)
    )
    assert repo.count_by_status()[RequestStatus.RECEIVED] >= 1
    done = repo.set_status(req.id, RequestStatus.VERIFIED, completed_at=NOW)
    assert done.completed_at == NOW
    purged = repo.purge_subject_key(req.id)
    assert not purged.has_subject_key and repo.subject_key(req.id) is None
    raw = session.execute(
        text("SELECT subject_key IS NULL FROM lae.erasure_request WHERE id = :i"), {"i": req.id}
    ).scalar_one()
    assert raw is True  # SQL NULL, not JSON null


def _plan(request_id: UUID, *, blocked: bool) -> ErasurePlan:
    edge = uuid4()
    items = (
        PlanItem(
            1,
            PlanAction.NULLIFY_REFERENCE,
            tref("customer"),
            LineageLevel.RECORD,
            ("id",),
            ((15,), (16,)),
            ("referrer_id",),
            2,
            (),
            (edge,),
            "detach referrals",
        ),
        PlanItem(
            2,
            PlanAction.DELETE_ROWS,
            tref("customer"),
            LineageLevel.RECORD,
            ("id",),
            ((7,),),
            (),
            1,
            (1,),
            (),
            "delete subject",
        ),
        PlanItem(
            3,
            PlanAction.REFRESH_MATVIEW,
            tref("customer_ltv"),
            LineageLevel.TABLE,
            (),
            (),
            reason="refresh",
        ),
        PlanItem(
            4,
            PlanAction.ANONYMIZE_ROWS,
            tref("order_line"),
            LineageLevel.RECORD,
            ("order_id", "line_no"),
            ((1, 1), (1, 2)),
            ("note",),
            2,
            reason="anon",
        ),
    )
    conflicts = (
        PlanConflict(
            ConflictCode.SHARED_RECORD,
            Severity.BLOCKING,
            tref("ticket_message"),
            "shared",
            resolved=not blocked,
        ),
        PlanConflict(
            ConflictCode.EXTERNAL_ARTIFACT,
            Severity.WARNING,
            TableRef("external", "crm"),
            "external",
        ),
    )
    return ErasurePlan(uuid4(), request_id, H1, H2, items, conflicts, H3)


def test_plan_round_trip_approval_and_purge(session: Session, snapshot: UUID) -> None:
    req = _request(session)
    repo = PlanRepo(session)
    assert repo.next_version(req.id) == 1
    plan = _plan(req.id, blocked=True)
    rec = repo.create(
        plan, snapshot_id=snapshot, version=1, record_count=6, record_key_hmacs={2: [H1]}
    )
    assert rec.status is PlanStatus.BLOCKED
    loaded = repo.get(plan.plan_id)
    assert loaded.items == plan.items
    assert sorted(loaded.conflicts, key=str) == sorted(plan.conflicts, key=str)
    assert loaded.is_blocked
    assert repo.items(plan.plan_id)[1].record_key_hmacs == (H1,)
    assert repo.item_ids(plan.plan_id).keys() == {1, 2, 3, 4}
    assert repo.live_for_request(req.id) == rec
    (shared,) = [
        c for c in repo.conflicts(plan.plan_id) if c.conflict.severity is Severity.BLOCKING
    ]
    resolved = repo.resolve_conflict(shared.id, "ACKNOWLEDGED", "dpo")
    assert resolved.conflict.resolved and not repo.get(plan.plan_id).is_blocked
    with pytest.raises(ValidationError):
        repo.resolve_conflict(shared.id, "IGNORED", "dpo")
    repo.set_status(plan.plan_id, PlanStatus.DRAFT)
    assert repo.set_plan_hash(plan.plan_id, "4" * 64).plan_hash == "4" * 64
    with pytest.raises(PlanHashMismatch):
        repo.approve(plan.plan_id, "dpo", NOW, H3)
    assert repo.approval(plan.plan_id) is None
    approved = repo.approve(plan.plan_id, "dpo", NOW, "4" * 64)
    assert approved.status is PlanStatus.APPROVED and approved.approved_at == NOW
    approval = repo.approval(plan.plan_id)
    assert approval is not None and approval.plan_hash == "4" * 64
    # record graph
    edge = fk_edge("customer", "customer_address", "id", "customer_id", OnDeleteAction.CASCADE)
    SnapshotRepo(session).save_edges(snapshot, [edge])
    deps = [
        RecordDependencyRow(
            "app.customer", "app.customer", H1, H1, RecordRole.SUBJECT_ROOT, 0, None, (7,), (7,)
        ),
        RecordDependencyRow(
            "app.customer",
            "app.customer_address",
            H1,
            H2,
            RecordRole.OWNED,
            1,
            edge.edge_id,
            (7,),
            (6,),
        ),
    ]
    assert repo.save_record_dependencies(plan.plan_id, deps) == 2
    assert repo.record_dependencies(plan.plan_id) == deps
    assert repo.purge_raw_keys(plan.plan_id) == 2 + 3
    purged = repo.record_dependencies(plan.plan_id)
    assert all(d.parent_key is None and d.child_key is None for d in purged)
    assert all(i.record_keys == () for i in repo.get(plan.plan_id).items)
    nulls = session.execute(
        text("SELECT count(*) FROM lae.plan_item WHERE plan_id = :p AND record_keys IS NULL"),
        {"p": plan.plan_id},
    ).scalar_one()
    assert nulls == 4
    assert repo.latest_for_request(req.id) == repo.record(plan.plan_id)
    assert [p.version for p in repo.list_for_request(req.id)] == [1]
    with pytest.raises(NotFoundError):
        repo.get(uuid4())


def test_execution_verification_evidence(session: Session, snapshot: UUID) -> None:
    req = _request(session)
    plans = PlanRepo(session)
    plan = _plan(req.id, blocked=False)
    plans.create(plan, snapshot_id=snapshot, version=1, record_count=6)
    ex = ExecutionRepo(session)
    dry = ex.start(plan.plan_id, ExecutionMode.DRY_RUN, started_at=NOW)
    ex.record_steps(
        dry.id,
        [StepResult(1, StepStatus.APPLIED, 2, 2, 5), StepResult(2, StepStatus.APPLIED, 1, 1, 3)],
    )
    dry = ex.finish(dry.id, ExecutionStatus.ROLLED_BACK, finished_at=NOW)
    assert [s.step_order for s in dry.steps] == [1, 2] and dry.result.mode is ExecutionMode.DRY_RUN
    with pytest.raises(ValidationError):
        ex.record_steps(dry.id, [StepResult(99, StepStatus.APPLIED, 0, 0, 0)])
    vr = VerificationRepo(session)
    with pytest.raises(InvalidStateTransition):
        vr.start(dry.id)
    live = ex.start(plan.plan_id, ExecutionMode.LIVE)
    live = ex.finish(live.id, ExecutionStatus.COMMITTED, finished_at=NOW)
    assert ex.committed_live(plan.plan_id) == live
    again = ex.start(plan.plan_id, ExecutionMode.LIVE)
    with pytest.raises(AlreadyExecutedError):
        ex.finish(again.id, ExecutionStatus.COMMITTED, finished_at=NOW)
    failed = ex.finish(
        again.id,
        ExecutionStatus.FAILED,
        finished_at=NOW,
        error=ErrorInfo("ALREADY_EXECUTED", "dup"),
    )
    assert failed.error == ErrorInfo("ALREADY_EXECUTED", "dup")
    assert len(ex.list_for_plan(plan.plan_id)) == 3
    assert ex.latest_for_plan(plan.plan_id, ExecutionMode.DRY_RUN) == dry
    run = vr.start(live.id)
    assert run.status is VerificationStatus.RUNNING
    report = VerificationReport(
        status=VerificationStatus.PASSED,
        checks=(
            CheckResult(CheckType.ROW_ABSENT, tref("customer"), None, 2, True, {"n": 0}, {"n": 0}),
            CheckResult(CheckType.RESIDUAL_VALUE_SCAN, None, None, None, True, {"hits": []}, {}),
            CheckResult(
                CheckType.DERIVED_CONSISTENT,
                tref("customer_ltv"),
                None,
                3,
                True,
                {"d": 0},
                {"d": 0},
            ),
        ),
        outcome=VerificationOutcome.PARTIAL,
        required_artifacts=4,
        verified_artifacts=3,
    )
    done = vr.complete(run.id, report, finished_at=NOW)
    assert done.report.status is report.status and done.outcome is VerificationOutcome.PARTIAL
    assert set(done.report.checks) == set(report.checks)
    assert [
        c.observed for c in done.report.checks if c.check_type is CheckType.RESIDUAL_VALUE_SCAN
    ] == [{"hits": []}]
    assert vr.latest_for_execution(live.id) == vr.get(run.id)
    assert vr.mark_error(vr.start(live.id).id, finished_at=NOW).status is VerificationStatus.ERROR
    with pytest.raises(ValidationError):
        bad = CheckResult(CheckType.ROW_ABSENT, None, None, 42, True)
        vr.complete(run.id, VerificationReport(VerificationStatus.PASSED, (bad,)), finished_at=NOW)
    # signing keys + evidence
    keys = SigningKeyRepo(session)
    k = keys.register("abcdef0123456789", "PEM-A")
    assert keys.register("abcdef0123456789", "PEM-A\n") == k
    with pytest.raises(ValidationError):
        keys.register("abcdef0123456789", "PEM-B")
    assert keys.active() == k and keys.get(k.key_id) == k and keys.list_keys() == [k]
    from lae.contracts.enums import AuditEventType
    from lae.db.audit_sink import PgAuditSink

    anchor = PgAuditSink(session).append(
        AuditEventType.EVIDENCE_ISSUED, "erasure_plan", plan.plan_id, "svc", {}
    )
    ev = EvidenceRepo(session).save(
        plan_id=plan.plan_id,
        verification_run_id=run.id,
        merkle_root=H1,
        leaf_count=2,
        leaves_canonical="[]",
        chain_anchor_seq=anchor.seq,
        chain_anchor_hash=anchor.event_hash,
        bundle_canonical="{}",
        signature="c2ln",
        signing_key_id=k.key_id,
        issued_at=NOW,
    )
    assert EvidenceRepo(session).for_plan(plan.plan_id) == ev == EvidenceRepo(session).get(ev.id)
    assert EvidenceRepo(session).for_plan(uuid4()) is None
    assert keys.retire(k.key_id, NOW).retired_at == NOW and keys.active() is None
    assert AuditRepo(session).for_entities([plan.plan_id], "erasure_plan")[-1] == anchor


def test_benchmarks(session: Session) -> None:
    repo = BenchmarkRepo(session)
    r = repo.add(
        "B1",
        params={"scale": "tiny"},
        seed=42,
        environment={"pg": "16"},
        metrics={"p50_ms": 12},
        git_commit="abc",
    )
    assert repo.list_runs("B1")[0] == r
    assert r in repo.list_runs()
    assert repo.list_runs("B9") == []
