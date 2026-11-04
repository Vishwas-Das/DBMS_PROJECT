"""PostgreSQL-backed implementation of LaeServicesProtocol (Lead-owned).

Write operations delegate to `lae.services.pipeline`. This module maps results to API models
and serves read-only queries. Every number returned comes from a live database query.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any
from uuid import UUID

from psycopg import sql
from sqlalchemy import text
from sqlalchemy.orm import Session

from lae.api.schemas import (
    AuditEventOut,
    AuditOut,
    BenchmarkRunOut,
    ChainVerificationOut,
    CheckOut,
    CustomerOut,
    DiscoveryOut,
    EdgeStatusUpdate,
    ErasureRequestCreate,
    ErasureRequestOut,
    ErrorBody,
    EvidenceOut,
    ExecutionOut,
    GraphEdgeOut,
    GraphNodeOut,
    HealthOut,
    LineageOut,
    PlanConflictOut,
    PlanExecute,
    PlanGenerate,
    PlanItemOut,
    PlanOut,
    RecordEdgeOut,
    RecordNodeOut,
    ResidualHitOut,
    SchemaGraphOut,
    StatsOut,
    StepResultOut,
    VerificationOut,
)
from lae.audit.evidence import first_envelope_mismatch
from lae.audit.hashchain import verify_chain
from lae.audit.signing import verify_signature
from lae.contracts.enums import (
    ActionCategory,
    CheckType,
    EdgeStatus,
    LineageLevel,
    RecordRole,
    RequestStatus,
    VerificationOutcome,
    action_category,
)
from lae.contracts.types import AuditEventRecord, RecordKey, VerificationReport
from lae.db.engine import session_scope
from lae.db.records import ExecutionRunRecord, RequestRecord, VerificationRunRecord
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
from lae.errors import NotFoundError
from lae.lineage import edge_evidence
from lae.planner import ACKNOWLEDGEABLE_CODES
from lae.services import pipeline as P  # noqa: N812 - short alias for the workflow module
from lae.services.runtime import Runtime


def _mask_email(email: str | None) -> str:
    if not email or "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    return f"{local[:1]}***@{domain}"


class LaeServices:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt

    # ------------------------------------------------------------------ health / stats
    def health(self) -> HealthOut:
        try:
            with session_scope(self.rt.reader, read_only=True) as s:
                ext: dict[str, str] = dict(
                    s.execute(
                        text(
                            "SELECT extname, extversion FROM pg_extension "
                            "WHERE extname IN ('vector','pgcrypto')"
                        )
                    ).all()
                )
                chain = verify_chain(AuditRepo(s).chain())
            ok = True
        except Exception:  # database unreachable -> degraded, never a 500
            return HealthOut(
                status="degraded",
                database=False,
                pgvector=None,
                pgcrypto=None,
                audit_chain_valid=None,
                signing_key_id=None,
            )
        return HealthOut(
            status="ok" if ok and chain.valid else "degraded",
            database=True,
            pgvector=ext.get("vector"),
            pgcrypto=ext.get("pgcrypto"),
            audit_chain_valid=chain.valid,
            signing_key_id=self.rt.signer.key_id if self.rt.signer else None,
        )

    def stats(self) -> StatsOut:
        with session_scope(self.rt.reader, read_only=True) as s:
            root = self._default_root(s)
            customers = s.execute(
                text(
                    sql.SQL("SELECT count(*) FROM {}.{}")
                    .format(sql.Identifier(root.table.schema), sql.Identifier(root.table.name))
                    .as_string()
                )
            ).scalar_one()
            by_status = RequestRepo(s).count_by_status()
            snap = SnapshotRepo(s).latest()
            return StatsOut(
                synthetic_customers=int(customers),
                erasure_requests=sum(by_status.values()),
                verified_requests=by_status.get(RequestStatus.VERIFIED, 0),
                partial_requests=by_status.get(RequestStatus.EXECUTED, 0),
                pending_requests=sum(
                    by_status.get(x, 0)
                    for x in (RequestStatus.RECEIVED, RequestStatus.PLANNED, RequestStatus.APPROVED)
                ),
                failed_requests=by_status.get(RequestStatus.FAILED, 0),
                discovered_tables=snap.table_count if snap else 0,
                discovered_edges=snap.edge_count if snap else 0,
                registered_artifacts=len(ArtifactRepo(s).list_artifacts()),
                audit_events=AuditRepo(s).count(),
                latest_snapshot_id=snap.id if snap else None,
                latest_snapshot_at=snap.created_at if snap else None,
            )

    def _default_root(self, s: Session) -> Any:
        roots = PolicyRepo(s).list_subject_roots()
        if not roots:
            raise NotFoundError("no subject root configured (seed the database first)")
        return roots[0].root

    def search_customers(self, q: str | None, limit: int) -> list[CustomerOut]:
        with session_scope(self.rt.reader, read_only=True) as s:
            root = self._default_root(s)
            t = sql.Identifier(root.table.schema, root.table.name)
            stmt = sql.SQL(
                "SELECT id, full_name, email FROM {t} "
                "WHERE %(q)s::text IS NULL OR full_name ILIKE %(like)s OR id::text = %(q)s "
                "ORDER BY id LIMIT %(n)s"
            ).format(t=t)
            like = f"%{q}%" if q else None
            raw = s.connection().connection.driver_connection
            assert raw is not None
            with raw.cursor() as cur:
                cur.execute(stmt, {"q": q, "like": like, "n": limit})
                rows = cur.fetchall()
            open_by_hmac: dict[str, UUID] = {}
            for r in RequestRepo(s).list_requests(limit=1000):
                if r.status not in (RequestStatus.VERIFIED, RequestStatus.CANCELLED):
                    open_by_hmac.setdefault(r.subject_key_hmac, r.id)
            out = []
            for cid, name, email in rows:
                h = self.rt.keyring.key_digest(RecordKey(root.table, root.key_columns, (cid,)))
                out.append(
                    CustomerOut(
                        id=int(cid),
                        display_name=str(name),
                        email_masked=_mask_email(email),
                        open_request_id=open_by_hmac.get(h),
                    )
                )
            return out

    # ------------------------------------------------------------------ requests
    def _request_out(self, s: Session, r: RequestRecord) -> ErasureRequestOut:
        root = PolicyRepo(s).get_subject_root(r.subject_root_id)
        plan = PlanRepo(s).latest_for_request(r.id)
        outcome: VerificationOutcome | None = None
        if plan is not None:
            latest = P.latest_verification_report(s, plan.id)
            if latest is not None:
                outcome = latest[1].outcome
        return ErasureRequestOut(
            id=r.id,
            subject_root=root.name,
            subject_key_hmac=r.subject_key_hmac,
            status=r.status,
            requested_by=r.requested_by,
            created_at=r.created_at,
            completed_at=r.completed_at,
            current_plan_id=plan.id if plan else None,
            latest_outcome=outcome,
        )

    def create_request(self, body: ErasureRequestCreate, actor: str) -> ErasureRequestOut:
        rid = P.create_request(
            self.rt, body.subject_root, tuple(body.subject_key), actor, body.reason
        )
        return self.get_request(rid)

    def list_requests(self, limit: int) -> list[ErasureRequestOut]:
        with session_scope(self.rt.reader, read_only=True) as s:
            return [self._request_out(s, r) for r in RequestRepo(s).list_requests(limit=limit)]

    def get_request(self, request_id: UUID) -> ErasureRequestOut:
        with session_scope(self.rt.reader, read_only=True) as s:
            return self._request_out(s, RequestRepo(s).get(request_id))

    # ------------------------------------------------------------------ discovery / lineage
    def _discovery_out(self, d: P.DiscoverySummary) -> DiscoveryOut:
        g = d.discovery.record_graph
        roles = Counter(a.role for _, a in g.nodes())
        return DiscoveryOut(
            request_id=d.request_id,
            snapshot_id=d.snapshot_id,
            schema_fingerprint=d.schema_fingerprint,
            tables=d.tables,
            table_edges=d.table_edges,
            column_edges=d.column_edges,
            records_found=len(g.nodes()),
            records_by_role={RecordRole(k): v for k, v in roles.items()},
            affected_artifacts={
                k.qualified(): v for k, v in d.discovery.affected_artifacts.items()
            },
            external_dependencies=sorted(a.target.qualified() for a in d.external),
            record_set_hash=g.record_set_hash(),
            duration_ms=d.duration_ms,
            warnings=list(d.discovery.warnings),
        )

    def run_discovery(self, request_id: UUID, actor: str) -> DiscoveryOut:
        return self._discovery_out(P.run_discovery(self.rt, request_id, actor))

    def latest_discovery(self, request_id: UUID) -> DiscoveryOut:
        # read-only recomputation (discovery results are not stored separately from plans)
        return self._discovery_out(P.run_discovery(self.rt, request_id, "reader", audit=False))

    def set_edge_status(self, edge_id: UUID, body: EdgeStatusUpdate, actor: str) -> GraphEdgeOut:
        e = P.set_edge_status(self.rt, edge_id, EdgeStatus(body.status), actor)
        return GraphEdgeOut(
            id=e.edge_id,
            source=f"t:{e.source.qualified()}",
            target=f"t:{e.target.qualified()}",
            level="TABLE" if e.level is LineageLevel.TABLE else "COLUMN",
            origin=e.origin,
            transform=e.transform,
            traceability=e.traceability,
            status=e.status,
            source_columns=list(e.source_columns),
            target_columns=list(e.target_columns),
            on_delete_action=e.on_delete.value if e.on_delete else None,
            confidence=float(e.confidence),
            evidence=edge_evidence(e),
        )

    def _schema_graph(self, s: Session, snapshot_id: UUID, level: str) -> SchemaGraphOut:
        ctx = P.load_lineage(self.rt, s, snapshot_id)
        lvl = LineageLevel(level)
        nl = ctx.graph.to_node_link(lvl)
        nodes = [
            GraphNodeOut(**{k: v for k, v in n.items() if k in GraphNodeOut.model_fields})
            for n in nl["nodes"]
        ]
        edges = [
            GraphEdgeOut(**{k: v for k, v in e.items() if k in GraphEdgeOut.model_fields})
            for e in nl["edges"]
        ]
        return SchemaGraphOut(
            snapshot_id=snapshot_id,
            level=level,
            nodes=nodes,
            edges=edges,
        )

    def schema_graph(self, level: str) -> SchemaGraphOut:
        sid = P.latest_or_new_snapshot(self.rt, "reader")
        with session_scope(self.rt.reader, read_only=True) as s:
            return self._schema_graph(s, sid, level)

    def lineage(self, request_id: UUID, level: str) -> LineageOut:
        with session_scope(self.rt.reader, read_only=True) as s:
            RequestRepo(s).get(request_id)
            plan = PlanRepo(s).latest_for_request(request_id)
            if plan is not None:
                schema = self._schema_graph(s, plan.snapshot_id, level)
                deps = PlanRepo(s).record_dependencies(plan.id)
                nodes: dict[str, RecordNodeOut] = {}
                edges: list[RecordEdgeOut] = []
                for dep in deps:
                    nodes.setdefault(
                        dep.child_key_hmac,
                        RecordNodeOut(
                            id=dep.child_key_hmac,
                            table=dep.child_table,
                            role=dep.child_role,
                            depth=dep.depth,
                        ),
                    )
                    if dep.edge_id is not None:
                        edges.append(
                            RecordEdgeOut(
                                source=dep.parent_key_hmac,
                                target=dep.child_key_hmac,
                                edge_id=dep.edge_id,
                            )
                        )
                return LineageOut(
                    request_id=request_id,
                    plan_id=plan.id,
                    snapshot_id=plan.snapshot_id,
                    schema_graph=schema,
                    records=sorted(nodes.values(), key=lambda n: (n.table, n.id)),
                    record_edges=edges,
                )
        d = P.run_discovery(self.rt, request_id, "reader", audit=False)
        with session_scope(self.rt.reader, read_only=True) as s:
            schema = self._schema_graph(s, d.snapshot_id, level)
        nl = d.discovery.record_graph.to_node_link(self.rt.keyring.key_digest)
        return LineageOut(
            request_id=request_id,
            plan_id=None,
            snapshot_id=d.snapshot_id,
            schema_graph=schema,
            records=[
                RecordNodeOut(
                    id=n["id"],
                    table=n["table"],
                    role=n["role"],
                    depth=n["depth"],
                    is_predicate=n.get("is_predicate", False),
                )
                for n in nl["nodes"]
            ],
            record_edges=[
                RecordEdgeOut(source=e["source"], target=e["target"], edge_id=e.get("edge_id"))
                for e in nl["edges"]
            ],
        )

    # ------------------------------------------------------------------ plans
    def get_plan(self, plan_id: UUID) -> PlanOut:
        with session_scope(self.rt.reader, read_only=True) as s:
            plans = PlanRepo(s)
            rec = plans.record(plan_id)
            plan = plans.get(plan_id)
            retention = {
                r.table: f"{r.action.value}" + (f" ({r.legal_basis})" if r.legal_basis else "")
                for r in PolicyRepo(s).list_retention()
            }
            items = [
                PlanItemOut(
                    id=ir.id,
                    step_order=it.step_order,
                    action=it.action,
                    category=action_category(it.action),
                    schema_name=it.table.schema,
                    table_name=it.table.name,
                    lineage_level="RECORD" if it.lineage_level is LineageLevel.RECORD else "TABLE",
                    key_columns=list(it.key_columns),
                    record_count=len(ir.record_key_hmacs) or len(it.record_keys),
                    record_key_hmacs=list(ir.record_key_hmacs),
                    target_columns=list(it.target_columns),
                    expected_row_count=it.expected_row_count,
                    depends_on_steps=list(it.depends_on),
                    reason_edge_ids=list(it.reason_edge_ids),
                    reason=it.reason,
                    retention_policy=retention.get(it.table),
                )
                for ir in plans.items(plan_id)
                for it in [ir.item]
            ]
            conflicts = [
                PlanConflictOut(
                    id=c.id,
                    code=c.conflict.code,
                    severity=c.conflict.severity,
                    schema_name=c.conflict.table.schema,
                    table_name=c.conflict.table.name,
                    message=c.conflict.message,
                    resolution=c.resolution,
                    acknowledgeable=c.conflict.code in ACKNOWLEDGEABLE_CODES
                    and c.resolution is None,
                )
                for c in plans.conflicts(plan_id)
            ]
            cats = Counter(i.category for i in items)
            return PlanOut(
                id=rec.id,
                request_id=rec.request_id,
                version=rec.version,
                status=rec.status,
                snapshot_id=rec.snapshot_id,
                schema_fingerprint=rec.schema_fingerprint,
                record_set_hash=rec.record_set_hash,
                plan_hash=rec.plan_hash,
                record_count=rec.record_count,
                items=items,
                conflicts=conflicts,
                counts_by_category={ActionCategory(k): v for k, v in cats.items()},
                is_blocked=plan.is_blocked,
                created_at=rec.created_at,
                approved_by=rec.approved_by,
                approved_at=rec.approved_at,
            )

    def generate_plan(self, request_id: UUID, body: PlanGenerate, actor: str) -> PlanOut:
        pid = P.generate_plan(
            self.rt, request_id, actor, max_depth=body.max_depth, max_records=body.max_records
        )
        return self.get_plan(pid)

    def acknowledge_conflict(
        self, plan_id: UUID, conflict_id: UUID, actor: str, note: str | None
    ) -> PlanOut:
        P.acknowledge(self.rt, plan_id, conflict_id, actor, note)
        return self.get_plan(plan_id)

    def approve_plan(self, plan_id: UUID, plan_hash: str, actor: str) -> PlanOut:
        P.approve(self.rt, plan_id, plan_hash, actor)
        return self.get_plan(plan_id)

    # ------------------------------------------------------------------ execution
    def _execution_out(
        self, s: Session, run: ExecutionRunRecord, timings: dict[str, int] | None = None
    ) -> ExecutionOut:
        items = {ir.item.step_order: ir.item for ir in PlanRepo(s).items(run.plan_id)}
        steps = [
            StepResultOut(
                step_order=st.step_order,
                action=items[st.step_order].action,
                category=action_category(items[st.step_order].action),
                table=items[st.step_order].table.qualified(),
                status=st.status,
                rows_affected=st.rows_affected,
                expected_rows=st.expected_rows,
                duration_ms=st.duration_ms,
            )
            for st in run.steps
            if st.step_order in items
        ]
        v = VerificationRepo(s).latest_for_execution(run.id)
        ev = EvidenceRepo(s).for_plan(run.plan_id) if v else None
        return ExecutionOut(
            id=run.id,
            plan_id=run.plan_id,
            mode=run.mode,
            status=run.status,
            started_at=run.started_at,
            finished_at=run.finished_at,
            steps=steps,
            error=ErrorBody(code=run.error.code, message=run.error.message) if run.error else None,
            verification=self._verification_out(v) if v else None,
            evidence_id=ev.id if ev else None,
            timings_ms=timings or {},
        )

    def execute_plan(self, plan_id: UUID, body: PlanExecute, actor: str) -> ExecutionOut:
        res = P.execute(self.rt, plan_id, body.mode, actor)
        with session_scope(self.rt.reader, read_only=True) as s:
            return self._execution_out(s, ExecutionRepo(s).get(res.run_id), res.timings_ms)

    def list_executions(self, request_id: UUID) -> list[ExecutionOut]:
        with session_scope(self.rt.reader, read_only=True) as s:
            out: list[ExecutionOut] = []
            for p in PlanRepo(s).list_for_request(request_id):
                out += [self._execution_out(s, r) for r in ExecutionRepo(s).list_for_plan(p.id)]
            return sorted(out, key=lambda e: e.started_at, reverse=True)

    # ------------------------------------------------------------------ verification
    def _verification_out(self, v: VerificationRunRecord) -> VerificationOut:
        report: VerificationReport = v.report
        hits: list[ResidualHitOut] = []
        for c in report.checks:
            if c.check_type is CheckType.RESIDUAL_VALUE_SCAN:
                hits += [
                    ResidualHitOut(table=h["table"], column=h["column"], count=h["count"])
                    for h in c.observed.get("hits", [])
                ]
        dur = int((v.finished_at - v.started_at).total_seconds() * 1000) if v.finished_at else None
        return VerificationOut(
            id=v.id,
            execution_run_id=v.execution_run_id,
            status=v.status,
            outcome=v.outcome,
            required_artifacts=report.required_artifacts,
            verified_artifacts=report.verified_artifacts,
            coverage=float(report.coverage),
            checks=[
                CheckOut(
                    check_type=c.check_type,
                    schema_name=c.table.schema if c.table else None,
                    table_name=c.table.name if c.table else None,
                    column_name=c.column,
                    step_order=c.step_order,
                    passed=c.passed,
                    observed=dict(c.observed),
                    expected=dict(c.expected),
                )
                for c in report.checks
            ],
            residual_hits=hits,
            started_at=v.started_at,
            finished_at=v.finished_at,
            duration_ms=dur,
        )

    def _latest_verification(self, s: Session, request_id: UUID) -> VerificationRunRecord:
        for p in PlanRepo(s).list_for_request(request_id)[::-1]:
            run = ExecutionRepo(s).committed_live(p.id)
            if run is not None:
                v = VerificationRepo(s).latest_for_execution(run.id)
                if v is not None:
                    return v
        raise NotFoundError("no verification for this request yet")

    def get_verification(self, request_id: UUID) -> VerificationOut:
        with session_scope(self.rt.reader, read_only=True) as s:
            return self._verification_out(self._latest_verification(s, request_id))

    def run_verification(self, request_id: UUID, actor: str) -> VerificationOut:
        with session_scope(self.rt.reader, read_only=True) as s:
            exec_id = self._latest_verification(s, request_id).execution_run_id
        vid = P.verify(self.rt, exec_id, actor)
        with session_scope(self.rt.reader, read_only=True) as s:
            return self._verification_out(VerificationRepo(s).get(vid))

    # ------------------------------------------------------------------ audit / evidence
    @staticmethod
    def _event_out(e: AuditEventRecord) -> AuditEventOut:
        payload = json.loads(e.payload_canonical)
        return AuditEventOut(
            seq=e.seq,
            event_id=_event_uuid(e),
            occurred_at=e.occurred_at,
            event_type=e.event_type,
            entity_type=e.entity_type,
            entity_id=e.entity_id,
            actor=e.actor,
            payload=payload.get("data", payload),
            prev_hash=e.prev_hash,
            event_hash=e.event_hash,
        )

    def _chain_out(self, s: Session) -> ChainVerificationOut:
        events = AuditRepo(s).chain()
        res = verify_chain(events)
        reason = getattr(res, "reason", None)
        valid = res.valid
        first_bad = res.first_invalid_seq
        bad_env = first_envelope_mismatch(events)  # actor/occurred_at/entity columns (CR crypto-3)
        if bad_env is not None and (first_bad is None or bad_env < first_bad):
            valid, first_bad, reason = False, bad_env, "ENVELOPE_MISMATCH"
        return ChainVerificationOut(
            valid=valid,
            checked_events=res.checked_events,
            head_seq=res.head_seq,
            head_hash=res.head_hash,
            first_invalid_seq=first_bad,
            reason=reason,
        )

    def audit_events(self, after_seq: int, limit: int) -> list[AuditEventOut]:
        with session_scope(self.rt.reader, read_only=True) as s:
            return [self._event_out(e) for e in AuditRepo(s).list_events(after_seq, limit)]

    def verify_chain(self) -> ChainVerificationOut:
        with session_scope(self.rt.reader, read_only=True) as s:
            return self._chain_out(s)

    def audit_for_request(self, request_id: UUID) -> AuditOut:
        with session_scope(self.rt.reader, read_only=True) as s:
            RequestRepo(s).get(request_id)
            ids: set[UUID] = {request_id}
            evidence = None
            for p in PlanRepo(s).list_for_request(request_id):
                ids.add(p.id)
                for r in ExecutionRepo(s).list_for_plan(p.id):
                    ids.add(r.id)
                    v = VerificationRepo(s).latest_for_execution(r.id)
                    if v:
                        ids.add(v.id)
                ev = EvidenceRepo(s).for_plan(p.id)
                if ev is not None:
                    ids.add(ev.id)
                    evidence = ev
            events = sorted(AuditRepo(s).for_entities(ids), key=lambda e: e.seq)
            ev_out = None
            if evidence is not None:
                key = SigningKeyRepo(s).get(evidence.signing_key_id)
                ev_out = EvidenceOut(
                    id=evidence.id,
                    plan_id=evidence.plan_id,
                    merkle_root=evidence.merkle_root,
                    leaf_count=evidence.leaf_count,
                    chain_anchor_seq=evidence.chain_anchor_seq,
                    chain_anchor_hash=evidence.chain_anchor_hash,
                    signing_key_id=evidence.signing_key_id,
                    issued_at=evidence.issued_at,
                    signature_valid=verify_signature(
                        key.public_key_pem,
                        evidence.bundle_canonical.encode("utf-8"),
                        evidence.signature,
                    ),
                    bundle=json.loads(evidence.bundle_canonical),
                    signature=evidence.signature,
                )
            return AuditOut(
                request_id=request_id,
                events=[self._event_out(e) for e in events],
                chain=self._chain_out(s),
                evidence=ev_out,
            )

    def _plan_with_evidence(self, request_id: UUID) -> UUID:
        with session_scope(self.rt.reader, read_only=True) as s:
            for p in PlanRepo(s).list_for_request(request_id)[::-1]:
                if EvidenceRepo(s).for_plan(p.id) is not None:
                    return p.id
        raise NotFoundError("no evidence issued for this request")

    def evidence_download(self, request_id: UUID) -> bytes:
        return P.evidence_export(self.rt, self._plan_with_evidence(request_id))[0]

    def evidence_leaves(self, request_id: UUID) -> bytes:
        return P.evidence_export(self.rt, self._plan_with_evidence(request_id))[1]

    # ------------------------------------------------------------------ benchmarks
    def benchmark_runs(self, scenario: str | None) -> list[BenchmarkRunOut]:
        with session_scope(self.rt.reader, read_only=True) as s:
            return [
                BenchmarkRunOut(
                    id=r.id,
                    scenario=r.scenario,
                    params=dict(r.params),
                    seed=r.seed,
                    git_commit=r.git_commit,
                    environment=dict(r.environment),
                    metrics=dict(r.metrics),
                    created_at=r.created_at,
                )
                for r in BenchmarkRepo(s).list_runs(scenario)
            ]


def _event_uuid(e: AuditEventRecord) -> UUID:
    # AuditEventRecord has no event_id field; derive a stable UUID from the hash for the API.
    return UUID(e.event_hash[:32])


def build_services() -> LaeServices:
    return LaeServices(Runtime.from_settings())
