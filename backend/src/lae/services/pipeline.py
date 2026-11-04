"""Core erasure workflow on PostgreSQL (Lead-owned integration layer).

snapshot → request → discover → plan → (acknowledge) → approve → execute (dry run | live)
→ verify → evidence → purge.

Every state change runs in an explicit transaction and appends its audit event in that same
transaction. Domain modules receive DB access only through the ports.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from lae.audit.anchor import export_chain_head
from lae.audit.canonical import canonical_json, sha256_hex
from lae.audit.evidence import (
    ChainAnchor,
    ExecutionEvidence,
    build_evidence,
    leaves_from_verification,
)
from lae.contracts.enums import (
    ArtifactKind,
    AuditEventType,
    EdgeStatus,
    ExecutionMode,
    ExecutionStatus,
    LineageLevel,
    PlanStatus,
    RecordRole,
    RefreshStrategy,
    RequestStatus,
    SnapshotStatus,
    TableKind,
    VerificationOutcome,
    VerificationStatus,
    action_category,
)
from lae.contracts.types import (
    Approval,
    ArtifactDefinition,
    CatalogSnapshot,
    ColumnRef,
    ErrorInfo,
    ExecutionResult,
    KeyTuple,
    RecordKey,
    RelationshipEdge,
    ResidualProbe,
    SubjectRoot,
    TableRef,
    VerificationReport,
)
from lae.db.audit_sink import PgAuditSink
from lae.db.catalog_queries import PgCatalogReader
from lae.db.engine import session_scope
from lae.db.records import RecordDependencyRow
from lae.db.repositories import (
    ArtifactRepo,
    AuditRepo,
    EvidenceRepo,
    ExecutionRepo,
    PlanRepo,
    PolicyRepo,
    RequestRepo,
    SigningKeyRepo,
    SnapshotRepo,
    VerificationRepo,
)
from lae.db.target import PgTargetReader, PgTargetWriter
from lae.discovery import (
    artifacts_from_catalog,
    capture_residual_probes,
    discover_foreign_keys,
    discover_records,
    external_dependencies,
    extract_artifacts_lineage,
    infer_relationships,
    inspectable_artifacts,
    merge_registered,
    read_catalog,
)
from lae.errors import (
    AlreadyExecutedError,
    ExecutionError,
    InvalidStateTransition,
    LaeError,
    NotFoundError,
    SubjectNotFoundError,
    TargetQueryError,
    ValidationError,
)
from lae.executor import execute_plan
from lae.lineage import LineageGraph, RecordDiscoveryResult, build_lineage_graph
from lae.planner import (
    acknowledge_conflict,
    build_plan,
    check_approval,
    compute_plan_hash,
    initial_status,
    pseudonym_columns,
)
from lae.services.runtime import Runtime
from lae.verification import verify_execution

log = logging.getLogger("lae.services")

TEXT_TYPES = ("text", "character varying", "varchar", "citext", "jsonb", "json", "character")


# =================================================================== value objects


@dataclass(frozen=True)
class LineageContext:
    snapshot_id: UUID
    catalog: CatalogSnapshot
    edges: tuple[RelationshipEdge, ...]
    graph: LineageGraph
    artifacts: tuple[ArtifactDefinition, ...]  # inspectable (in-DB) artifacts
    external: tuple[ArtifactDefinition, ...]  # EXTERNAL, not inspectable


@dataclass(frozen=True)
class DiscoverySummary:
    request_id: UUID
    snapshot_id: UUID
    schema_fingerprint: str
    tables: int
    table_edges: int
    column_edges: int
    discovery: RecordDiscoveryResult
    external: tuple[ArtifactDefinition, ...]
    duration_ms: int


@dataclass(frozen=True)
class ExecutionOutcome:
    run_id: UUID
    plan_id: UUID
    mode: ExecutionMode
    status: ExecutionStatus
    result: ExecutionResult | None
    error: ErrorInfo | None
    verification_run_id: UUID | None = None
    evidence_id: UUID | None = None
    timings_ms: dict[str, int] = field(default_factory=dict)


# =================================================================== helpers


@contextmanager
def _timed(timings: dict[str, int], name: str) -> Iterator[None]:
    t0 = time.perf_counter()
    try:
        yield
    finally:
        timings[name] = int((time.perf_counter() - t0) * 1000)


def _subject(rt: Runtime, s: Session, subject_root_id: UUID) -> SubjectRoot:
    return PolicyRepo(s).get_subject_root(subject_root_id).root


def _scan_columns(catalog: CatalogSnapshot) -> list[ColumnRef]:
    """All text-like columns of tables and materialized views (residual-scan scope)."""
    cols: list[ColumnRef] = []
    for ref in sorted(catalog.tables):
        info = catalog.tables[ref]
        if info.kind is TableKind.VIEW:
            continue  # views are recomputed from base tables; scanning them would double count
        for c in info.columns:
            base = c.data_type.split("(")[0].strip()
            if base in TEXT_TYPES:
                cols.append(ColumnRef(ref, c.name))
    return cols


def _extra_keys(
    catalog: CatalogSnapshot, fk_edges: Sequence[RelationshipEdge], root_table: TableRef | None
) -> dict[TableRef, list[tuple[str, ...]]]:
    """FK child columns referencing the subject chain are candidate keys for SQL lineage
    (CR lineage-discovery-engineer-1 §2). Restricting to the chain avoids treating e.g. a
    product-level aggregate as subject-owned."""
    if root_table is None:
        return {}
    table_edges = [e for e in fk_edges if e.level is LineageLevel.TABLE]
    graph = build_lineage_graph(catalog, fk_edges)
    chain = {root_table} | graph.reachable_from(root_table)
    out: dict[TableRef, list[tuple[str, ...]]] = {}
    for e in table_edges:
        if e.source in chain:
            out.setdefault(e.target, []).append(tuple(e.target_columns))
    return out


def _key_hmac(rt: Runtime, key: RecordKey) -> str:
    return rt.keyring.key_digest(key)


def _subject_record_key(root: SubjectRoot, key: KeyTuple) -> RecordKey:
    return RecordKey(root.table, root.key_columns, key)


# =================================================================== snapshot / lineage


def create_snapshot(rt: Runtime, actor: str = "system") -> UUID:
    """Scan the catalog, discover FK + SQL + registered-mapping lineage; persist READY snapshot."""
    schemas = tuple(rt.settings.target_schemas)
    with session_scope(rt.owner) as s:
        cat_reader = PgCatalogReader(s)
        catalog = read_catalog(cat_reader, schemas)
        fk_edges = discover_foreign_keys(cat_reader, catalog)
        roots = PolicyRepo(s).list_subject_roots()
        root_table = roots[0].root.table if roots else None
        registered = ArtifactRepo(s).active_definitions()
        artifacts = merge_registered(artifacts_from_catalog(catalog), registered)
        inspectable = inspectable_artifacts(artifacts, catalog)
        sql_results = extract_artifacts_lineage(
            inspectable, catalog, extra_keys=_extra_keys(catalog, fk_edges, root_table)
        )
        sql_edges = [e for r in sql_results for e in r.edges]
        warnings = [
            {"artifact_id": str(r.artifact_id), "warning": w}
            for r in sql_results
            for w in r.warnings
        ]
        confirmed = [*fk_edges, *sql_edges]
        inferred = infer_relationships(catalog, cat_reader, exclude=confirmed)
        # keep previously confirmed/rejected decisions on inferred edges (ids are deterministic)
        prev = SnapshotRepo(s).latest()
        decided: dict[UUID, EdgeStatus] = {}
        carried: list[RelationshipEdge] = []
        if prev is not None:
            for e in SnapshotRepo(s).load_edges(prev.id):
                if e.origin.value != "INFERRED":
                    continue
                if e.status is not EdgeStatus.SUGGESTED:
                    decided[e.edge_id] = e.status
                # residual-scan suggestions are not re-derivable from the catalog: carry them
                if (
                    e.details.get("evidence") == "residual_scan"
                    and catalog.has_table(e.source)
                    and catalog.has_table(e.target)
                ):
                    carried.append(e)
        inferred = [_with_status(e, decided.get(e.edge_id, e.status)) for e in inferred]
        seen = {e.edge_id for e in inferred}
        inferred += [e for e in carried if e.edge_id not in seen]
        all_edges = [*confirmed, *inferred]

        snaps = SnapshotRepo(s)
        snap = snaps.create(schemas, catalog.fingerprint)
        snaps.save_catalog(snap.id, catalog)
        snaps.save_edges(snap.id, all_edges)
        snaps.set_status(
            snap.id,
            SnapshotStatus.READY,
            table_count=len(catalog.tables),
            edge_count=len(all_edges),
            warnings=warnings,
        )
        PgAuditSink(s, clock=rt.clock).append(
            AuditEventType.SNAPSHOT_CREATED,
            "lineage_snapshot",
            snap.id,
            actor,
            {
                "schema_fingerprint": catalog.fingerprint,
                "tables": len(catalog.tables),
                "fk_edges": len(fk_edges),
                "sql_edges": len(sql_edges),
                "inferred_edges": len(inferred),
                "warnings": len(warnings),
            },
        )
        return snap.id


def _with_status(e: RelationshipEdge, status: EdgeStatus) -> RelationshipEdge:
    if e.status is status:
        return e
    from dataclasses import replace

    return replace(e, status=status)


def load_lineage(rt: Runtime, s: Session, snapshot_id: UUID) -> LineageContext:
    snaps = SnapshotRepo(s)
    catalog = snaps.load_catalog(snapshot_id)
    edges = tuple(snaps.load_edges(snapshot_id))
    policies = PolicyRepo(s).policy_set()
    registered = ArtifactRepo(s).active_definitions()
    artifacts = merge_registered(artifacts_from_catalog(catalog), registered)
    inspectable = tuple(inspectable_artifacts(artifacts, catalog))
    external = tuple(external_dependencies(artifacts, catalog))
    graph = build_lineage_graph(
        catalog, edges, policies.pii, artifacts=[a.target for a in inspectable]
    )
    return LineageContext(snapshot_id, catalog, edges, graph, inspectable, external)


def latest_snapshot(rt: Runtime) -> UUID:
    """Read paths only: the latest READY snapshot, never creating one (review M6)."""
    with session_scope(rt.reader, read_only=True) as s:
        latest = SnapshotRepo(s).latest()
    if latest is None:
        raise NotFoundError("no lineage snapshot yet; run discovery first")
    return latest.id


def latest_or_new_snapshot(rt: Runtime, actor: str) -> UUID:
    """Reuse the latest snapshot only if the live catalog fingerprint still matches it."""
    with session_scope(rt.reader, read_only=True) as s:
        latest = SnapshotRepo(s).latest()
        live_fp = read_catalog(PgCatalogReader(s), tuple(rt.settings.target_schemas)).fingerprint
    if latest is not None and latest.schema_fingerprint == live_fp:
        return latest.id
    return create_snapshot(rt, actor)


def set_edge_status(rt: Runtime, edge_id: UUID, status: EdgeStatus, actor: str) -> RelationshipEdge:
    """Human confirmation/rejection of an INFERRED edge (only those may change)."""
    with session_scope(rt.owner) as s:
        snaps = SnapshotRepo(s)
        edge = snaps.get_edge(edge_id)
        if edge.origin.value != "INFERRED":
            raise ValidationError("only INFERRED edges can be confirmed or rejected")
        if edge.status is status:
            return edge  # idempotent: no state change, no audit event
        updated = snaps.set_edge_status(edge_id, status)
        PgAuditSink(s, clock=rt.clock).append(
            AuditEventType.EDGE_STATUS_CHANGED,
            "lineage_edge",
            edge_id,
            actor,
            {
                "from": edge.status.value,
                "to": status.value,
                "source": edge.source.qualified(),
                "target": edge.target.qualified(),
            },
        )
        return updated


# =================================================================== requests


def create_request(
    rt: Runtime, root_name: str, subject_key: KeyTuple, actor: str, reason: str | None
) -> UUID:
    snapshot_id = latest_or_new_snapshot(rt, actor)
    with session_scope(rt.owner) as s:
        rec = PolicyRepo(s).subject_root_by_name(root_name)
        if rec is None:
            raise NotFoundError(f"unknown subject root {root_name!r}")
        root = rec.root
        if len(subject_key) != len(root.key_columns):
            raise ValidationError("subject key arity does not match the subject root key")
        catalog = SnapshotRepo(s).load_catalog(snapshot_id)
        reader = PgTargetReader(s, catalog)
        try:
            rows = reader.select_by_columns(
                root.table, root.key_columns, [subject_key], select=root.key_columns
            )
        except TargetQueryError:
            # QA D2: a key that cannot be cast to the key column's type is invalid input (422),
            # never a server or state error. The value is not echoed.
            raise ValidationError("subject key does not match the subject root key type") from None
        if len(rows) != 1:
            raise SubjectNotFoundError("no such data subject")
        # review M4: canonicalise from the DB row ('42', '042' and 42 are the same subject)
        subject_key = tuple(rows[0][c] for c in root.key_columns)
        key_hmac = _key_hmac(rt, _subject_record_key(root, subject_key))
        for other in RequestRepo(s).find_by_hmac(root.root_id, key_hmac):
            if other.status not in (RequestStatus.VERIFIED, RequestStatus.CANCELLED):
                raise InvalidStateTransition(
                    "an open erasure request already exists for this subject",
                    details={"request_id": str(other.id)},
                )
        req = RequestRepo(s).create(root.root_id, subject_key, key_hmac, actor, reason)
        PgAuditSink(s, clock=rt.clock).append(
            AuditEventType.REQUEST_RECEIVED,
            "erasure_request",
            req.id,
            actor,
            {"subject_root": root_name, "subject_key_hmac": key_hmac},
        )
        return req.id


def _request_subject(s: Session, request_id: UUID) -> tuple[SubjectRoot, KeyTuple]:
    req = RequestRepo(s).get(request_id)
    key = RequestRepo(s).subject_key(request_id)
    if key is None:
        raise InvalidStateTransition("subject key already purged; this request is closed")
    return PolicyRepo(s).get_subject_root(req.subject_root_id).root, key


# =================================================================== discovery + planning


def run_discovery(
    rt: Runtime, request_id: UUID, actor: str, *, audit: bool = True
) -> DiscoverySummary:
    """audit=False is the read-only path (GET): it never creates a snapshot or writes."""
    t0 = time.perf_counter()
    snapshot_id = latest_or_new_snapshot(rt, actor) if audit else latest_snapshot(rt)
    with session_scope(rt.reader, isolation="REPEATABLE READ", read_only=True) as s:
        root, key = _request_subject(s, request_id)
        ctx = load_lineage(rt, s, snapshot_id)
        policies = PolicyRepo(s).policy_set()
        reader = PgTargetReader(s, ctx.catalog)
        result = discover_records(ctx.graph, root, key, reader, rt.limits, policies)
    ms = int((time.perf_counter() - t0) * 1000)
    tl = sum(1 for e in ctx.edges if e.level is LineageLevel.TABLE)
    summary = DiscoverySummary(
        request_id,
        snapshot_id,
        ctx.catalog.fingerprint,
        len(ctx.catalog.tables),
        tl,
        len(ctx.edges) - tl,
        result,
        ctx.external,
        ms,
    )
    if audit:
        with session_scope(rt.owner) as s:
            PgAuditSink(s, clock=rt.clock).append(
                AuditEventType.DISCOVERY_COMPLETED,
                "erasure_request",
                request_id,
                actor,
                {
                    "snapshot_id": str(snapshot_id),
                    "record_set_hash": result.record_graph.record_set_hash(),
                    "records": len(result.record_graph.nodes()),
                    "affected_artifacts": len(result.affected_artifacts),
                    "duration_ms": ms,
                },
            )
    return summary


def generate_plan(
    rt: Runtime,
    request_id: UUID,
    actor: str,
    *,
    max_depth: int | None = None,
    max_records: int | None = None,
) -> UUID:
    from lae.contracts.types import DiscoveryLimits

    limits = DiscoveryLimits(
        max_depth or rt.limits.max_depth, max_records or rt.limits.max_records, rt.limits.batch_size
    )
    snapshot_id = latest_or_new_snapshot(rt, actor)
    with session_scope(rt.reader, isolation="REPEATABLE READ", read_only=True) as s:
        root, key = _request_subject(s, request_id)
        ctx = load_lineage(rt, s, snapshot_id)
        policies = PolicyRepo(s).policy_set()
        reader = PgTargetReader(s, ctx.catalog)
        discovery = discover_records(ctx.graph, root, key, reader, limits, policies)
        probes = capture_residual_probes(
            root, key, discovery.record_graph, reader, policies, rt.probe_keyring
        )
        probes, ambiguous = _exclude_ambiguous_probes(rt, reader, ctx.catalog, root, key, probes)
        predicate_counts = {
            k: reader.count_by_columns(k.table, k.columns, [k.values])
            for k, _ in discovery.record_graph.nodes()
            if k.is_predicate
        }
    plan = build_plan(
        request_id,
        discovery,
        ctx.graph,
        policies,
        ctx.artifacts,
        ctx.external,
        plan_id=uuid4(),
        predicate_row_counts=predicate_counts,
    )

    with session_scope(rt.owner) as s:
        plans = PlanRepo(s)
        live = plans.live_for_request(request_id)
        if live is not None:
            plans.set_status(live.id, PlanStatus.SUPERSEDED)
            plans.purge_raw_keys(live.id)  # review H3: superseded versions keep HMACs only
        hmacs = {
            it.step_order: [
                _key_hmac(rt, RecordKey(it.table, it.key_columns, k, False)) for k in it.record_keys
            ]
            for it in plan.items
            if it.key_columns and it.record_keys
        }
        status = initial_status(plan)
        rec = plans.create(
            plan,
            snapshot_id=snapshot_id,
            version=plans.next_version(request_id),
            record_count=len(discovery.record_graph.nodes()),
            status=status,
            record_key_hmacs=hmacs,
        )
        plans.save_record_dependencies(rec.id, _dependency_rows(rt, discovery))
        RequestRepo(s).add_probes(request_id, probes)
        RequestRepo(s).set_status(request_id, RequestStatus.PLANNED)
        counts: dict[str, int] = {}
        for it in plan.items:
            counts[action_category(it.action).value] = (
                counts.get(action_category(it.action).value, 0) + 1
            )
        PgAuditSink(s, clock=rt.clock).append(
            AuditEventType.PLAN_CREATED,
            "erasure_plan",
            rec.id,
            actor,
            {
                "request_id": str(request_id),
                "plan_hash": plan.plan_hash,
                "record_set_hash": plan.record_set_hash,
                "status": status.value,
                "items": len(plan.items),
                "conflicts": len(plan.conflicts),
                "counts_by_category": counts,
                "residual_probes": len(probes),
                "ambiguous_probes_excluded": ambiguous,
            },
        )
        return rec.id


def _exclude_ambiguous_probes(
    rt: Runtime,
    reader: PgTargetReader,
    catalog: CatalogSnapshot,
    root: SubjectRoot,
    key: KeyTuple,
    probes: list[ResidualProbe],
) -> tuple[list[ResidualProbe], int]:
    """Drop probes whose value also identifies ANOTHER row of the subject-root table.

    A residual hit on a value shared with another person (e.g. two customers named
    "Ulla Dunmore") is not evidence that the subject's data survived, so such probes would make
    a correct erasure FAILED. A probe is ambiguous if its matches across the root table's
    text-like columns exceed its matches in the subject's own root row. The subject row is
    read once here and its values never leave this function. Only counts are kept.
    Trade-off (documented): non-unique identifiers such as names are not residual-scanned.
    """
    info = catalog.table(root.table)
    text_cols = [c.name for c in info.columns if c.data_type.split("(")[0].strip() in TEXT_TYPES]
    if not text_cols or not probes:
        return probes, 0
    row = reader.fetch_columns(root.table, root.key_columns, key, text_cols) or {}
    kept: list[ResidualProbe] = []
    for p in probes:
        own = sum(
            1
            for col in text_cols
            if isinstance(row.get(col), str)
            and rt.probe_keyring.digest(row[col], p.normalization) == p.value_hmac
        )
        total = sum(
            reader.hmac_match_count(
                ColumnRef(root.table, col), [p.value_hmac], p.normalization, rt.probe_key
            )
            for col in text_cols
        )
        if total <= own:
            kept.append(p)
    return kept, len(probes) - len(kept)


def _dependency_rows(rt: Runtime, d: RecordDiscoveryResult) -> list[RecordDependencyRow]:
    g = d.record_graph
    attrs = dict(g.nodes())
    rows: list[RecordDependencyRow] = []
    for k, a in g.nodes():
        if a.role is RecordRole.SUBJECT_ROOT:
            h = _key_hmac(rt, k)
            rows.append(
                RecordDependencyRow(
                    k.table.qualified(),
                    k.table.qualified(),
                    h,
                    h,
                    a.role,
                    0,
                    None,
                    k.values,
                    k.values,
                )
            )
    for parent, child, edge_id in g.edges():
        rows.append(
            RecordDependencyRow(
                parent.table.qualified(),
                child.table.qualified(),
                _key_hmac(rt, parent),
                _key_hmac(rt, child),
                attrs[child].role,
                attrs[child].depth,
                edge_id,
                parent.values,
                child.values,
            )
        )
    return rows


def acknowledge(
    rt: Runtime, plan_id: UUID, conflict_id: UUID, actor: str, note: str | None = None
) -> None:
    with session_scope(rt.owner) as s:
        plans = PlanRepo(s)
        rec = plans.record(plan_id)
        if rec.status not in (PlanStatus.DRAFT, PlanStatus.BLOCKED):
            raise InvalidStateTransition(f"cannot acknowledge conflicts on a {rec.status} plan")
        conflicts = plans.conflicts(plan_id)
        idx = next((i for i, c in enumerate(conflicts) if c.id == conflict_id), None)
        if idx is None:
            raise NotFoundError("conflict not found on this plan")
        new_plan = acknowledge_conflict(plans.get(plan_id), idx)  # raises if not acknowledgeable
        plans.resolve_conflict(conflict_id, "ACKNOWLEDGED", actor)
        plans.set_plan_hash(plan_id, new_plan.plan_hash)
        if rec.status is PlanStatus.BLOCKED and not new_plan.is_blocked:
            plans.set_status(plan_id, PlanStatus.DRAFT)
        PgAuditSink(s, clock=rt.clock).append(
            AuditEventType.CONFLICT_ACKNOWLEDGED,
            "erasure_plan",
            plan_id,
            actor,
            {
                "conflict_id": str(conflict_id),
                "code": conflicts[idx].conflict.code.value,
                "new_plan_hash": new_plan.plan_hash,
                "note_present": bool(note),
            },
        )


def approve(rt: Runtime, plan_id: UUID, plan_hash: str, actor: str) -> None:
    with session_scope(rt.owner) as s:
        plans = PlanRepo(s)
        rec = plans.record(plan_id)
        plan = plans.get(plan_id)
        if compute_plan_hash(plan) != rec.plan_hash:
            raise InvalidStateTransition("stored plan content does not match its hash")
        check_approval(plan, rec.status, plan_hash)
        plans.approve(plan_id, actor, rt.clock(), plan_hash)
        RequestRepo(s).set_status(rec.request_id, RequestStatus.APPROVED)
        PgAuditSink(s, clock=rt.clock).append(
            AuditEventType.PLAN_APPROVED,
            "erasure_plan",
            plan_id,
            actor,
            {"plan_hash": plan_hash, "request_id": str(rec.request_id)},
        )


# =================================================================== execution


def _recompute_sql(ctx: LineageContext) -> dict[TableRef, str]:
    return {
        a.target: a.definition_sql
        for a in ctx.artifacts
        if a.refresh_strategy is RefreshStrategy.RECOMPUTE_SQL and a.definition_sql
    }


def _allowed_sql(ctx: LineageContext) -> list[str]:
    return [a.definition_sql for a in ctx.artifacts if a.definition_sql]


_EXECUTION_LOCK = "lae.live_execution"  # distinct from the audit sink's chain lock key


@contextmanager
def _live_execution_lock(rt: Runtime) -> Iterator[None]:
    """Serialise LIVE executions BEFORE their SERIALIZABLE transaction takes its snapshot.

    QA D1: two concurrent executions both read the same audit-chain head inside their
    snapshots. uq_audit_event_prev_hash (migration 0003) makes a fork impossible, but then
    *neither* run could commit. Holding a session-level advisory lock on a separate
    connection for the whole execution means the second run starts after the first has
    committed, sees a fresh snapshot, and fails cleanly (already executed / stale).
    """
    from sqlalchemy import text as sa_text

    with rt.owner.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(sa_text("SELECT pg_advisory_lock(hashtext(:k))"), {"k": _EXECUTION_LOCK})
        try:
            yield
        finally:
            conn.execute(sa_text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": _EXECUTION_LOCK})


def execute(
    rt: Runtime, plan_id: UUID, mode: ExecutionMode, actor: str, *, auto_verify: bool = True
) -> ExecutionOutcome:
    if mode is ExecutionMode.LIVE:
        with _live_execution_lock(rt):
            outcome = _execute(rt, plan_id, mode, actor, auto_verify=False)
        if auto_verify:  # verification + evidence run outside the execution lock
            outcome = _verify_and_issue(rt, outcome, actor)
        return outcome
    return _execute(rt, plan_id, mode, actor, auto_verify=auto_verify)


def _verify_and_issue(rt: Runtime, outcome: ExecutionOutcome, actor: str) -> ExecutionOutcome:
    timings = dict(outcome.timings_ms)
    with _timed(timings, "verification"):
        vid = verify(rt, outcome.run_id, actor)
    with _timed(timings, "evidence"):
        eid = issue_evidence(rt, outcome.plan_id, actor)
    return ExecutionOutcome(
        outcome.run_id,
        outcome.plan_id,
        outcome.mode,
        outcome.status,
        outcome.result,
        None,
        vid,
        eid,
        timings,
    )


def _execute(
    rt: Runtime, plan_id: UUID, mode: ExecutionMode, actor: str, *, auto_verify: bool = True
) -> ExecutionOutcome:
    timings: dict[str, int] = {}
    with session_scope(rt.owner) as s:
        plans = PlanRepo(s)
        rec = plans.record(plan_id)
        plan = plans.get(plan_id)
        if mode is ExecutionMode.LIVE:
            if rec.status is not PlanStatus.APPROVED:
                raise InvalidStateTransition("only an APPROVED plan can be executed LIVE")
            if ExecutionRepo(s).committed_live(plan_id) is not None:
                raise AlreadyExecutedError("this plan was already executed")
            approval = plans.approval(plan_id)
            if approval is None:  # explicit check (asserts vanish under python -O, review L3)
                raise InvalidStateTransition("plan has no recorded approval")
            if rt.settings.require_four_eyes and approval.approved_by == actor:
                raise InvalidStateTransition(
                    "separation of duties: the approver may not execute the plan",
                    details={"rule": "four_eyes"},
                )
        else:
            if rec.status not in (PlanStatus.DRAFT, PlanStatus.APPROVED):
                raise InvalidStateTransition(f"cannot dry-run a {rec.status} plan")
            approval = Approval(plan_id, plan.plan_hash, f"dry-run:{actor}", rt.clock())
        root, key = _request_subject(s, rec.request_id)
        run = ExecutionRepo(s).start(plan_id, mode, started_at=rt.clock())
        PgAuditSink(s, clock=rt.clock).append(
            AuditEventType.EXECUTION_STARTED,
            "execution_run",
            run.id,
            actor,
            {"plan_id": str(plan_id), "mode": mode.value, "plan_hash": plan.plan_hash},
        )
    run_id = run.id

    result: ExecutionResult | None = None
    error: LaeError | None = None
    try:
        with _timed(timings, "execution"), session_scope(rt.owner, isolation="SERIALIZABLE") as s:
            ctx = load_lineage(rt, s, rec.snapshot_id)
            policies = PolicyRepo(s).policy_set()
            writer = PgTargetWriter(
                s, ctx.catalog, hmac_key=rt.pseudonym_key, allowed_sql=_allowed_sql(ctx)
            )
            schemas = tuple(rt.settings.target_schemas)

            def rediscover(reader: Any) -> tuple[str, str]:
                fp = read_catalog(PgCatalogReader(s), schemas).fingerprint
                rs = discover_records(
                    ctx.graph, root, key, reader, rt.limits, policies
                ).record_graph.record_set_hash()
                return fp, rs

            result = execute_plan(
                plan,
                approval,
                writer,
                rediscover,
                PgAuditSink(s, clock=rt.clock),
                mode,
                clock=rt.clock,
                pseudonym_columns=pseudonym_columns(plan, policies, ctx.graph),
                recompute_sql=_recompute_sql(ctx),
                plan_hasher=compute_plan_hash,
                actor=actor,
            )
            if mode is ExecutionMode.LIVE:
                ex = ExecutionRepo(s)
                ex.record_steps(run_id, result.steps)
                ex.finish(run_id, ExecutionStatus.COMMITTED, finished_at=rt.clock())
                PlanRepo(s).set_status(plan_id, PlanStatus.EXECUTED)
                RequestRepo(s).set_status(rec.request_id, RequestStatus.EXECUTED)
            else:
                s.rollback()  # DRY RUN: every change (data + audit append) is discarded
    except LaeError as exc:
        error = exc
    except Exception as exc:  # review L3: never leave a run RUNNING on an unexpected error
        log.error("execution failed unexpectedly", extra={"type": type(exc).__name__})
        error = ExecutionError(
            "execution failed unexpectedly",
            code="EXECUTION_FAILED",
            details={"type": type(exc).__name__},
        )

    if mode is ExecutionMode.DRY_RUN or error is not None:
        status = (
            ExecutionStatus.ROLLED_BACK
            if error is None
            else (
                ExecutionStatus.ROLLED_BACK
                if mode is ExecutionMode.DRY_RUN
                else ExecutionStatus.FAILED
            )
        )
        info = ErrorInfo(error.code, error.message) if error else None
        with session_scope(rt.owner) as s:
            ex = ExecutionRepo(s)
            if result is not None:
                ex.record_steps(run_id, result.steps)
            ex.finish(run_id, status, finished_at=rt.clock(), error=info)
            etype = (
                AuditEventType.EXECUTION_ROLLED_BACK
                if mode is ExecutionMode.DRY_RUN
                else AuditEventType.EXECUTION_FAILED
            )
            PgAuditSink(s, clock=rt.clock).append(
                etype,
                "execution_run",
                run_id,
                actor,
                {
                    "plan_id": str(plan_id),
                    "mode": mode.value,
                    "error_code": error.code if error else None,
                    "steps": len(result.steps) if result else 0,
                },
            )
        if error is not None:
            error.details.setdefault("execution_run_id", str(run_id))
            raise error
        return ExecutionOutcome(run_id, plan_id, mode, status, result, None, timings_ms=timings)

    outcome = ExecutionOutcome(
        run_id, plan_id, mode, ExecutionStatus.COMMITTED, result, None, timings_ms=timings
    )
    if auto_verify:
        with _timed(timings, "verification"):
            vid = verify(rt, run_id, actor)
        with _timed(timings, "evidence"):
            eid = issue_evidence(rt, plan_id, actor)
        outcome = ExecutionOutcome(
            run_id, plan_id, mode, ExecutionStatus.COMMITTED, result, None, vid, eid, timings
        )
    return outcome


# =================================================================== verification


def verify(rt: Runtime, execution_run_id: UUID, actor: str) -> UUID:
    with session_scope(rt.owner) as s:
        run = ExecutionRepo(s).get(execution_run_id)
        if run.mode is not ExecutionMode.LIVE or run.status is not ExecutionStatus.COMMITTED:
            raise InvalidStateTransition("only a committed LIVE execution can be verified")
        vrun = VerificationRepo(s).start(execution_run_id, started_at=rt.clock())
        prec = PlanRepo(s).record(run.plan_id)
    try:
        with session_scope(rt.reader, isolation="REPEATABLE READ", read_only=True) as s:
            plan = PlanRepo(s).get(run.plan_id)
            root, key = _request_subject(s, prec.request_id)
            ctx = load_lineage(rt, s, prec.snapshot_id)
            policies = PolicyRepo(s).policy_set()
            probes = RequestRepo(s).list_probes(prec.request_id)
            reader = PgTargetReader(s, ctx.catalog, allowed_sql=_allowed_sql(ctx))

            def rediscover_records(r: Any) -> RecordDiscoveryResult:
                return discover_records(ctx.graph, root, key, r, rt.limits, policies)

            execution = ExecutionRepo(s).get(execution_run_id).result
            report = verify_execution(
                plan,
                execution,
                reader,
                rediscover_records,
                list(ctx.artifacts),
                probes,
                _scan_columns(ctx.catalog),
                rt.probe_key,
                policies,
            )
    except LaeError:
        with session_scope(rt.owner) as s:
            VerificationRepo(s).mark_error(vrun.id, finished_at=rt.clock())
        raise

    with session_scope(rt.owner) as s:
        VerificationRepo(s).complete(vrun.id, report, finished_at=rt.clock())
        _suggest_from_residual_hits(rt, s, prec.snapshot_id, root, report, actor)
        req_status = {
            VerificationOutcome.VERIFIED: RequestStatus.VERIFIED,
            VerificationOutcome.PARTIAL: RequestStatus.EXECUTED,
            VerificationOutcome.FAILED: RequestStatus.FAILED,
        }[report.outcome]
        if report.outcome is VerificationOutcome.VERIFIED:
            PlanRepo(s).set_status(run.plan_id, PlanStatus.VERIFIED)
        RequestRepo(s).set_status(
            prec.request_id,
            req_status,
            completed_at=rt.clock() if req_status is RequestStatus.VERIFIED else None,
        )
        failed = sorted({c.check_type.value for c in report.checks if not c.passed})
        PgAuditSink(s, clock=rt.clock).append(
            AuditEventType.VERIFICATION_COMPLETED,
            "verification_run",
            vrun.id,
            actor,
            {
                "execution_run_id": str(execution_run_id),
                "status": report.status.value,
                "outcome": report.outcome.value,
                "required_artifacts": report.required_artifacts,
                "verified_artifacts": report.verified_artifacts,
                "failed_check_types": failed,
                "checks": len(report.checks),
            },
        )
    return vrun.id


_RESIDUAL_NS = UUID("6f1d3c52-6a39-4f0e-9a51-7e3b2c1d0a99")


def _suggest_from_residual_hits(
    rt: Runtime,
    s: Session,
    snapshot_id: UUID,
    root: SubjectRoot,
    report: VerificationReport,
    actor: str,
) -> list[RelationshipEdge]:
    """Feed residual-scan hits back into lineage as SUGGESTED edges (architecture §12).

    The scan only knows (table, column, count), never values. The suggested mapping is
    root.<identifier column> -> hit column, preferring an identifier column with the same name.
    A human must CONFIRM it before discovery traverses it.
    """
    from uuid import uuid5

    from lae.contracts.enums import CheckType, EdgeOrigin, Traceability, Transform

    hits: list[dict[str, Any]] = []
    for c in report.checks:
        if c.check_type is CheckType.RESIDUAL_VALUE_SCAN and not c.passed:
            hits.extend(c.observed.get("hits", []))
    if not hits or not root.identifier_columns:
        return []
    snaps = SnapshotRepo(s)
    catalog = snaps.load_catalog(snapshot_id)
    loaded = snaps.load_edges(snapshot_id)
    existing = {e.edge_id for e in loaded}
    same_pair = {
        (e.level, e.source, e.source_columns, e.target, e.target_columns)
        for e in loaded
        if e.origin.value == "INFERRED"
    }
    new: list[RelationshipEdge] = []
    for h in hits:
        target = TableRef.parse(h["table"])
        col = str(h["column"])
        if target == root.table or not catalog.has_table(target):
            continue
        sources = [c for c in root.identifier_columns if c == col] or list(root.identifier_columns)
        for src in sources:
            for level in (LineageLevel.TABLE, LineageLevel.COLUMN):
                eid = uuid5(_RESIDUAL_NS, f"{level}|{root.table}.{src}|{target}.{col}")
                if eid in existing or (level, root.table, (src,), target, (col,)) in same_pair:
                    continue  # already suggested (e.g. by containment inference)
                new.append(
                    RelationshipEdge(
                        edge_id=eid,
                        level=level,
                        origin=EdgeOrigin.INFERRED,
                        source=root.table,
                        target=target,
                        source_columns=(src,),
                        target_columns=(col,),
                        transform=Transform.IDENTITY,
                        traceability=Traceability.KEY_PRESERVING,
                        status=EdgeStatus.SUGGESTED,
                        confidence=0.5,
                        details={
                            "evidence": "residual_scan",
                            "hit_count": int(h.get("count", 0)),
                            "note": "value match found by the lineage-independent scan",
                        },
                    )
                )
                existing.add(eid)
    if new:
        snaps.save_edges(snapshot_id, new)
        PgAuditSink(s, clock=rt.clock).append(
            AuditEventType.EDGE_STATUS_CHANGED,
            "lineage_snapshot",
            snapshot_id,
            actor,
            {
                "suggested_edges": len(new),
                "evidence": "residual_scan",
                "targets": sorted({e.target.qualified() for e in new}),
            },
        )
    return new


def latest_verification_report(s: Session, plan_id: UUID) -> tuple[UUID, VerificationReport] | None:
    run = ExecutionRepo(s).committed_live(plan_id)
    if run is None:
        return None
    v = VerificationRepo(s).latest_for_execution(run.id)
    if v is None or v.report is None:
        return None
    return v.id, v.report


# =================================================================== evidence + purge


def issue_evidence(rt: Runtime, plan_id: UUID, actor: str) -> UUID:
    signer = rt.require_signer()
    with session_scope(rt.owner) as s:
        existing = EvidenceRepo(s).for_plan(plan_id)
        if existing is not None:
            return existing.id
        plan = PlanRepo(s).get(plan_id)
        prec = PlanRepo(s).record(plan_id)
        run = ExecutionRepo(s).committed_live(plan_id)
        if run is None or run.finished_at is None:
            raise InvalidStateTransition("no committed LIVE execution for this plan")
        latest = latest_verification_report(s, plan_id)
        if latest is None:
            raise InvalidStateTransition("plan has no completed verification")
        vrun_id, report = latest
        if report.status is VerificationStatus.ERROR:
            raise InvalidStateTransition("verification errored; evidence not issued")
        SigningKeyRepo(s).register(signer.key_id, signer.public_key_pem)
        head = AuditRepo(s).head()
        assert head is not None
        leaves = leaves_from_verification(plan, report, rt.keyring)
        bundle = build_evidence(
            plan,
            ExecutionEvidence(run.id, run.finished_at, run.result),
            report,
            leaves,
            ChainAnchor(head.seq, head.event_hash),
            signer,
            rt.clock(),
        )
        rec = EvidenceRepo(s).save(
            plan_id=plan_id,
            verification_run_id=vrun_id,
            merkle_root=bundle.merkle_root,
            leaf_count=bundle.leaf_count,
            leaves_canonical=bundle.leaves_canonical,
            chain_anchor_seq=head.seq,
            chain_anchor_hash=head.event_hash,
            bundle_canonical=bundle.bundle_canonical,
            signature=bundle.signature,
            signing_key_id=signer.key_id,
            issued_at=bundle.issued_at,
        )
        sink = PgAuditSink(s, clock=rt.clock)
        sink.append(
            AuditEventType.EVIDENCE_ISSUED,
            "evidence_bundle",
            rec.id,
            actor,
            {
                "plan_id": str(plan_id),
                "merkle_root": bundle.merkle_root,
                "bundle_sha256": sha256_hex(bundle.bundle_canonical.encode("utf-8")),
                "outcome": report.outcome.value,
                "signing_key_id": signer.key_id,
            },
        )
        if report.outcome is VerificationOutcome.VERIFIED:
            RequestRepo(s).purge_subject_key(prec.request_id)
            # review H3: every plan version of the request, not only the executed one
            purged = sum(
                PlanRepo(s).purge_raw_keys(p.id)
                for p in PlanRepo(s).list_for_request(prec.request_id)
            )
            sink.append(
                AuditEventType.SUBJECT_DATA_PURGED,
                "erasure_request",
                prec.request_id,
                actor,
                {"plan_id": str(plan_id), "purged_rows": purged},
            )
        evidence_id = rec.id
    export_anchor(rt, actor)
    return evidence_id


def export_anchor(rt: Runtime, actor: str) -> str:
    """Sign the current chain head and write it OUTSIDE the database (external audit anchor)."""
    signer = rt.require_signer()
    with session_scope(rt.owner) as s:
        head = AuditRepo(s).head()
        if head is None:
            raise InvalidStateTransition("audit chain is empty")
        anchor = export_chain_head(head, signer, rt.clock())
        rt.anchor_dir.mkdir(parents=True, exist_ok=True)
        path = rt.anchor_dir / f"anchor-{head.seq:08d}.json"
        path.write_bytes(anchor.export_json().encode("utf-8"))
        PgAuditSink(s, clock=rt.clock).append(
            AuditEventType.CHAIN_HEAD_EXPORTED,
            "audit_event",
            None,
            actor,
            {"anchored_seq": head.seq, "anchored_hash": head.event_hash, "file": path.name},
        )
        return str(path)


def evidence_export(rt: Runtime, plan_id: UUID) -> tuple[bytes, bytes]:
    """(bundle file `{"bundle","signature"}`, leaves file) as canonical JSON bytes."""
    with session_scope(rt.reader, read_only=True) as s:
        rec = EvidenceRepo(s).for_plan(plan_id)
        if rec is None:
            raise NotFoundError("no evidence issued for this plan")
        bundle = json.loads(rec.bundle_canonical)
        return (
            canonical_json({"bundle": bundle, "signature": rec.signature}),
            rec.leaves_canonical.encode("utf-8"),
        )


def artifact_kind_label(a: ArtifactDefinition) -> str:
    return a.kind.value if isinstance(a.kind, ArtifactKind) else str(a.kind)
