"""The three erasure methods compared by the benchmark.

* ``ManualDeletion`` (BASELINE 1): the fixed script a developer would hard-code.
* ``FkOnlyDeletion`` (BASELINE 2): declared-FK discovery from ``lae.discovery`` only, then a
  child-first DELETE of every discovered row (no SQL lineage, no registered mappings, no
  residual scan, no retention or sharing policies).
* ``LaePipeline`` (PROPOSED): ``lae.services.pipeline`` snapshot -> request -> discovery -> plan
  -> (acknowledge) -> approve -> LIVE execute -> verify -> evidence.

Each ``run_subject`` returns timings (ms, float), the discovered record set (for precision /
recall) and a status. Measurement is done by the caller with ``benchmarks.measure``.
"""

from __future__ import annotations

import json
import logging
import secrets
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy import create_engine, text

from benchmarks import measure
from benchmarks.dbsetup import pg_url, sa_url

log = logging.getLogger("benchmarks.methods")


@dataclass
class MethodResult:
    status: str  # COMMITTED | FAILED | BLOCKED
    timings_ms: dict[str, float]
    discovered: frozenset[measure.Unit]
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@contextmanager
def timed(timings: dict[str, float], name: str) -> Iterator[None]:
    t0 = time.perf_counter()
    try:
        yield
    finally:
        timings[name] = (time.perf_counter() - t0) * 1000.0


def _sqlstate(exc: BaseException) -> str:
    diag = getattr(exc, "diag", None)
    code = getattr(exc, "sqlstate", None) or "?"
    cons = getattr(diag, "constraint_name", None) if diag else None
    return f"{type(exc).__name__}[{code}]" + (f" constraint={cons}" if cons else "")


# ====================================================================== BASELINE 1


MANUAL_SCRIPT: tuple[tuple[str, str, str], ...] = (
    (
        "app.customer_address",
        "SELECT id FROM app.customer_address WHERE customer_id = %(s)s",
        "DELETE FROM app.customer_address WHERE customer_id = %(s)s",
    ),
    (
        "app.loyalty_account",
        "SELECT customer_id FROM app.loyalty_account WHERE customer_id = %(s)s",
        "DELETE FROM app.loyalty_account WHERE customer_id = %(s)s",
    ),
    (
        "app.ticket_message",
        "SELECT id FROM app.ticket_message WHERE ticket_id IN "
        "(SELECT id FROM app.support_ticket WHERE customer_id = %(s)s)",
        "DELETE FROM app.ticket_message WHERE ticket_id IN "
        "(SELECT id FROM app.support_ticket WHERE customer_id = %(s)s)",
    ),
    (
        "app.support_ticket",
        "SELECT id FROM app.support_ticket WHERE customer_id = %(s)s",
        "DELETE FROM app.support_ticket WHERE customer_id = %(s)s",
    ),
    (
        "app.document",
        "SELECT id FROM app.document WHERE customer_id = %(s)s",
        "DELETE FROM app.document WHERE customer_id = %(s)s",
    ),
    (
        "app.customer",
        "SELECT id FROM app.customer WHERE id = %(s)s",
        "DELETE FROM app.customer WHERE id = %(s)s",
    ),
)


class ManualDeletion:
    name = "BASELINE_1_MANUAL"
    description = (
        "hard-coded DELETEs of customer_address, loyalty_account, ticket_message (via the "
        "subject's tickets), support_ticket, document, customer in ONE transaction; no "
        "knowledge of derived artifacts, caches, embeddings, hidden copies or retention"
    )

    def __init__(self, url: str) -> None:
        self.url = url

    def prepare(self) -> dict[str, float]:
        return {}

    def run_subject(self, customer_id: int) -> MethodResult:
        timings: dict[str, float] = {}
        found: set[measure.Unit] = set()
        params = {"s": customer_id}
        with psycopg.connect(pg_url(self.url)) as conn:
            t_total = time.perf_counter()
            with timed(timings, "discovery"):
                for table, select, _delete in MANUAL_SCRIPT:
                    found.update(
                        measure.unit(table, r) for r in conn.execute(select, params).fetchall()
                    )
                conn.commit()
            status, error = "COMMITTED", None
            deleted: dict[str, int] = {}
            with timed(timings, "execution"):
                try:
                    with conn.transaction():
                        for table, _select, delete in MANUAL_SCRIPT:
                            deleted[table] = conn.execute(delete, params).rowcount
                except psycopg.Error as exc:
                    status, error = "FAILED", _sqlstate(exc)
                    deleted = {}
            timings["total"] = (time.perf_counter() - t_total) * 1000.0
        return MethodResult(status, timings, frozenset(found), error, {"deleted": deleted})


# ====================================================================== BASELINE 2


class FkOnlyDeletion:
    name = "BASELINE_2_FK_ONLY"
    description = (
        "lae.discovery declared-FK edges only -> lae record discovery (BFS downstream) -> "
        "DELETE every discovered SUBJECT_ROOT/OWNED/SHARED row child-first in ONE "
        "transaction; other subjects' rows are not deleted (their SET NULL FK handles them)"
    )
    DELETE_ROLES = frozenset({"SUBJECT_ROOT", "OWNED", "SHARED"})

    def __init__(self, url: str) -> None:
        self.url = url
        self.engine = create_engine(sa_url(url), future=True, hide_parameters=True)
        self.catalog: Any = None
        self.graph: Any = None

    def prepare(self) -> dict[str, float]:
        from lae.db.catalog_queries import PgCatalogReader
        from lae.db.engine import session_scope
        from lae.discovery import discover_foreign_keys, read_catalog
        from lae.lineage import build_lineage_graph

        timings: dict[str, float] = {}
        with (
            timed(timings, "snapshot"),
            session_scope(self.engine, read_only=True) as s,
        ):
            reader = PgCatalogReader(s)
            self.catalog = read_catalog(reader, ("app",))
            fk = discover_foreign_keys(reader, self.catalog)
            self.graph = build_lineage_graph(self.catalog, fk)
        return timings

    def run_subject(self, customer_id: int) -> MethodResult:
        from lae.contracts.types import DiscoveryLimits, SubjectRoot, TableRef
        from lae.db.engine import session_scope
        from lae.db.target import PgTargetReader
        from lae.discovery import discover_records

        timings: dict[str, float] = {}
        root = SubjectRoot(uuid4(), TableRef("app", "customer"), ("id",), ())
        t_total = time.perf_counter()
        with (
            timed(timings, "discovery"),
            session_scope(self.engine, isolation="REPEATABLE READ", read_only=True) as s,
        ):
            res = discover_records(
                self.graph,
                root,
                (customer_id,),
                PgTargetReader(s, self.catalog),
                DiscoveryLimits(),
            )
            rg = res.record_graph
            order = rg.deletion_order(lambda _eid: False).order
            targets = [k for k in order if rg.attrs(k).role.value in self.DELETE_ROLES]
        with psycopg.connect(pg_url(self.url)) as conn:
            discovered = measure.resolve_record_graph(conn, rg.nodes(), {"SUBJECT_ROOT", "OWNED"})
            conn.commit()
            status, error, deleted = "COMMITTED", None, 0
            with timed(timings, "execution"):
                try:
                    with conn.transaction():
                        for k in targets:
                            q = sql.SQL("DELETE FROM {t} WHERE {c}").format(
                                t=sql.Identifier(k.table.schema, k.table.name),
                                c=sql.SQL(" AND ").join(
                                    sql.SQL("{} = %s").format(sql.Identifier(c)) for c in k.columns
                                ),
                            )
                            deleted += conn.execute(q, list(k.values)).rowcount
                except psycopg.Error as exc:
                    status, error, deleted = "FAILED", _sqlstate(exc), 0
        timings["total"] = (time.perf_counter() - t_total) * 1000.0
        roles: dict[str, int] = {}
        for _k, a in rg.nodes():
            roles[a.role.value] = roles.get(a.role.value, 0) + 1
        return MethodResult(
            status,
            timings,
            discovered,
            error,
            {"deleted_rows": deleted, "targets": len(targets), "roles": roles},
        )

    def close(self) -> None:
        self.engine.dispose()


# ====================================================================== PROPOSED


class LaePipeline:
    name = "PROPOSED_LAE"
    description = (
        "lae.services.pipeline: create_request -> run_discovery -> generate_plan -> acknowledge "
        "acknowledgeable BLOCKING conflicts -> approve(plan_hash) -> execute LIVE "
        "(SERIALIZABLE, drift re-check) -> verify -> issue_evidence"
    )

    def __init__(self, url: str, actor: str = "benchmark") -> None:
        from lae.audit.signing import Signer
        from lae.config import Settings
        from lae.services.runtime import Runtime

        self.url = url
        self.actor = actor
        self.approver = f"{actor}-approver"  # four-eyes: approver != executor
        self._tmp = tempfile.TemporaryDirectory(prefix="lae-bench-")
        key_path = Path(self._tmp.name) / "signing_key.pem"
        Signer.generate().save(key_path)
        u = sa_url(url)
        # _env_file=None: the repo .env (dev DB) must never leak into the benchmark runtime.
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            database_url=u,
            reader_url=u,
            executor_url=u,
            hmac_key=secrets.token_hex(32),
            signing_key_path=key_path,
            target_schemas=["app"],
            statement_timeout_ms=600_000,
        )
        self.rt = Runtime.from_settings(settings)
        self.rt.anchor_dir = Path(self._tmp.name) / "anchors"

    def prepare(self) -> dict[str, float]:
        from lae.services import pipeline

        timings: dict[str, float] = {}
        with timed(timings, "snapshot"):
            pipeline.create_snapshot(self.rt, self.actor)
        return timings

    def residual_suggestions(self, hit_columns: set[tuple[str, str]] | None = None) -> list[Any]:
        """SUGGESTED INFERRED edges the residual scan points at.

        Either edges created by the scan (evidence=residual_scan) or, since 6cf6abb dedups
        suggestions, an existing SUGGESTED INFERRED edge (e.g. containment inference) whose
        target (table, column) was a residual-scan hit.
        """
        from lae.contracts.enums import EdgeStatus
        from lae.db.engine import session_scope
        from lae.db.repositories import SnapshotRepo

        hits = hit_columns or set()
        with session_scope(self.rt.reader, read_only=True) as s:
            latest = SnapshotRepo(s).latest()
            if latest is None:
                return []
            out = []
            for e in SnapshotRepo(s).load_edges(latest.id, statuses=[EdgeStatus.SUGGESTED]):
                if e.origin.value != "INFERRED":
                    continue
                by_scan = e.details.get("evidence") == "residual_scan"
                pointed = any((e.target.qualified(), c) in hits for c in e.target_columns)
                if by_scan or pointed:
                    out.append(e)
            return out

    def confirm_edges(self, edges: list[Any]) -> int:
        from lae.contracts.enums import EdgeStatus
        from lae.services import pipeline

        for e in edges:
            pipeline.set_edge_status(self.rt, e.edge_id, EdgeStatus.CONFIRMED, self.actor)
        return len(edges)

    def run_subject(self, customer_id: int) -> MethodResult:
        from lae.contracts.enums import ExecutionMode, Severity
        from lae.db.engine import session_scope
        from lae.db.repositories import PlanRepo, VerificationRepo
        from lae.errors import LaeError
        from lae.services import pipeline

        rt, actor = self.rt, self.actor
        timings: dict[str, float] = {}
        details: dict[str, Any] = {}
        t_total = time.perf_counter()
        with timed(timings, "request"):
            rid = pipeline.create_request(rt, "customer", (customer_id,), actor, "benchmark")
        with timed(timings, "discovery"):
            summary = pipeline.run_discovery(rt, rid, actor)
        with psycopg.connect(pg_url(self.url)) as conn:
            discovered = measure.resolve_record_graph(
                conn, summary.discovery.record_graph.nodes(), {"SUBJECT_ROOT", "OWNED"}
            )
        with timed(timings, "planning"):
            pid = pipeline.generate_plan(rt, rid, actor)
        acked = 0
        with timed(timings, "acknowledge"):
            with session_scope(rt.reader, read_only=True) as s:
                conflicts = PlanRepo(s).conflicts(pid)
            for c in conflicts:
                if c.conflict.severity is Severity.BLOCKING and not c.conflict.resolved:
                    try:
                        pipeline.acknowledge(rt, pid, c.id, actor, "benchmark: auto-acknowledge")
                        acked += 1
                    except LaeError:
                        pass  # not acknowledgeable: needs a human policy decision
        with session_scope(rt.reader, read_only=True) as s:
            rec = PlanRepo(s).record(pid)
            plan = PlanRepo(s).get(pid)
            created = s.execute(
                text(
                    "SELECT payload_canonical FROM lae.audit_event "
                    "WHERE event_type = 'PLAN_CREATED' AND entity_id = :p ORDER BY seq LIMIT 1"
                ),
                {"p": pid},
            ).scalar()
        payload = json.loads(created) if created else {}
        payload = payload.get("data", payload)
        details["residual_probes"] = payload.get("residual_probes")
        details["ambiguous_probes_excluded"] = payload.get("ambiguous_probes_excluded")
        details.update(
            plan_items=len(plan.items),
            plan_conflicts=sorted(
                f"{c.code.value}:{c.severity.value}" + (":ack" if c.resolved else "")
                for c in plan.conflicts
            ),
            acknowledged=acked,
            plan_status=rec.status.value,
        )
        if plan.is_blocked:
            timings["total"] = (time.perf_counter() - t_total) * 1000.0
            return MethodResult("BLOCKED", timings, discovered, "PLAN_BLOCKED", details)
        status, error = "COMMITTED", None
        with timed(timings, "approval"):
            pipeline.approve(rt, pid, rec.plan_hash, self.approver)  # four-eyes
        try:
            with timed(timings, "execution"):
                outcome = pipeline.execute(rt, pid, ExecutionMode.LIVE, actor, auto_verify=False)
            with timed(timings, "verification"):
                vid = pipeline.verify(rt, outcome.run_id, actor)
            with timed(timings, "evidence"):
                pipeline.issue_evidence(rt, pid, actor)
            with session_scope(rt.reader, read_only=True) as s:
                v = VerificationRepo(s).get(vid)
            report = v.report
            assert report is not None
            residual = [
                {
                    "table": h.get("table"),
                    "column": h.get("column"),
                    "count": h.get("count"),
                }
                for c in report.checks
                if c.check_type.value == "RESIDUAL_VALUE_SCAN" and not c.passed
                for h in c.observed.get("hits", [])
            ]
            details.update(
                verification_status=report.status.value,
                outcome=report.outcome.value,
                lae_required_artifacts=report.required_artifacts,
                lae_verified_artifacts=report.verified_artifacts,
                failed_checks=sorted({c.check_type.value for c in report.checks if not c.passed}),
                lae_residual_hits=residual,
            )
        except LaeError as exc:
            status, error = "FAILED", exc.code
        timings["total"] = (time.perf_counter() - t_total) * 1000.0
        return MethodResult(status, timings, discovered, error, details)

    def close(self) -> None:
        self.rt.dispose()
        self._tmp.cleanup()


MethodFactory = Callable[[str], Any]
METHODS: dict[str, MethodFactory] = {
    ManualDeletion.name: ManualDeletion,
    FkOnlyDeletion.name: FkOnlyDeletion,
    LaePipeline.name: LaePipeline,
}
