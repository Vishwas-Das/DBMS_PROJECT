"""Fixtures for verification integration tests (owned by the Verification & Crypto Engineer).

These tests run against a REAL PostgreSQL database reserved for this suite
(``LAE_TEST_DATABASE_URL=...localhost:5433/lae_test_verif``). ``migrated_url`` is overridden
here so the shared fixtures (``migrated_db``, ``seeded_db``, ``app_conn``) accept that database;
any name other than ``lae_test`` / ``lae_test_verif`` is refused.

The tests play the executor's role by hand: :class:`World` builds contract ``PlanItem`` /
``ErasurePlan`` objects from the seeded data, applies their DML with ``PgTargetWriter`` inside
the rolled-back ``app_conn`` transaction, and then calls ``verify_execution`` with the real
``PgTargetReader`` on the same transaction.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar
from uuid import UUID, uuid5

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from lae.audit.canonical import canonical_hash
from lae.audit.hmac_keys import HmacKeyring
from lae.contracts.enums import (
    ExecutionMode,
    ExecutionStatus,
    LineageLevel,
    PlanAction,
    StepStatus,
)
from lae.contracts.types import (
    ArtifactDefinition,
    CatalogSnapshot,
    ColumnAssignment,
    ColumnRef,
    DiscoveryLimits,
    ErasurePlan,
    ExecutionResult,
    KeyTuple,
    PlanItem,
    PolicySet,
    ResidualProbe,
    StepResult,
    SubjectRoot,
    TableRef,
    VerificationReport,
)
from lae.db._sql import is_text_like
from lae.db.catalog_queries import PgCatalogReader
from lae.db.repositories.artifact import ArtifactRepo
from lae.db.repositories.policy import PolicyRepo
from lae.db.target import PgTargetReader, PgTargetWriter
from lae.discovery.catalog import read_catalog
from lae.discovery.foreign_keys import discover_foreign_keys
from lae.discovery.records import capture_residual_probes, discover_records
from lae.lineage.graph import LineageGraph, build_lineage_graph
from lae.lineage.record_graph import RecordGraph
from lae.lineage.results import RecordDiscoveryResult
from lae.verification import verify_execution

ALLOWED_DATABASES = frozenset({"lae_test", "lae_test_verif"})
HMAC_KEY = b"verification-integration-hmac-key-0123456789"  # fixed test key (>= 32 bytes)
NS = UUID("6f1c3c9e-6a55-4b8f-9b3e-0d7c2a1e5f00")
NULL = ColumnAssignment("NULL")
PSEUDO = ColumnAssignment("PSEUDONYM")


@pytest.fixture(scope="session")
def migrated_url(pg_url: str) -> str:
    """Override of the shared guard: this suite runs on its own ``lae_test_verif`` database."""
    url = pg_url
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    if make_url(url).database not in ALLOWED_DATABASES:
        pytest.fail(
            f"verification integration tests only run against {sorted(ALLOWED_DATABASES)}",
            pytrace=False,
        )
    return url


def tref(name: str) -> TableRef:
    return TableRef("app", name)


CUSTOMER = tref("customer")
ADDRESS = tref("customer_address")
LOYALTY = tref("loyalty_account")
ORDER = tref("purchase_order")
LINE = tref("order_line")
SHIPMENT = tref("shipment")
PAYMENT = tref("payment")
TICKET = tref("support_ticket")
MESSAGE = tref("ticket_message")
DOCUMENT = tref("document")
EMBEDDING = tref("document_embedding")
CACHE = tref("customer_profile_cache")
MARKETING = tref("marketing_event_log")
LTV = tref("customer_ltv")
REVENUE = tref("daily_revenue")
SEGMENT = tref("customer_segment")
CRM_EXPORT = TableRef("external", "crm_export_files")


def empty_rediscovery(_reader: Any) -> RecordDiscoveryResult:
    """For single-check tests that are not about REDISCOVERY_EMPTY."""
    return RecordDiscoveryResult(RecordGraph(), {})


@dataclass
class World:
    conn: Connection
    session: Session
    catalog: CatalogSnapshot
    reader: PgTargetReader
    writer: PgTargetWriter
    truth: Any
    policies: PolicySet
    artifacts: list[ArtifactDefinition]
    root: SubjectRoot
    graph: LineageGraph
    keyring: HmacKeyring
    subject: int
    assignments: dict[int, dict[str, ColumnAssignment]] = field(default_factory=dict)

    NULL: ClassVar[ColumnAssignment] = NULL
    PSEUDO: ClassVar[ColumnAssignment] = PSEUDO
    HMAC_KEY: ClassVar[bytes] = HMAC_KEY

    # ------------------------------------------------------------ data access (test only)
    def q(self, sql: str, **params: Any) -> list[tuple[Any, ...]]:
        """Bound-parameter SQL for test bookkeeping (never interpolated)."""
        return [tuple(r) for r in self.conn.execute(text(sql), params)]

    def keys(self, sql: str, **params: Any) -> tuple[KeyTuple, ...]:
        return tuple(sorted(self.q(sql, **params)))

    def count(self, sql: str, **params: Any) -> int:
        return int(self.q(sql, **params)[0][0])

    def artifact(self, target: TableRef) -> ArtifactDefinition:
        return next(a for a in self.artifacts if a.target == target)

    # ------------------------------------------------------------ plan construction
    @staticmethod
    def item(
        step: int,
        action: PlanAction,
        table: TableRef,
        key_columns: Sequence[str] = (),
        keys: Sequence[KeyTuple] = (),
        targets: Sequence[str] = (),
        *,
        level: LineageLevel = LineageLevel.RECORD,
        expected: int | None = None,
    ) -> PlanItem:
        record = level is LineageLevel.RECORD
        return PlanItem(
            step_order=step,
            action=action,
            table=table,
            lineage_level=level,
            key_columns=tuple(key_columns),
            record_keys=tuple(keys),
            target_columns=tuple(sorted(targets)),
            expected_row_count=(len(keys) if expected is None else expected) if record else None,
            reason="hand-built integration plan item",
        )

    def plan(self, items: Sequence[PlanItem]) -> ErasurePlan:
        body = [
            {
                "step": it.step_order,
                "action": it.action,
                "table": it.table.qualified(),
                "keys": len(it.record_keys),
            }
            for it in items
        ]
        return ErasurePlan(
            plan_id=uuid5(NS, canonical_hash(body)),
            request_id=uuid5(NS, f"request-{self.subject}"),
            schema_fingerprint=self.catalog.fingerprint,
            record_set_hash=canonical_hash({"subject_rows": [len(i.record_keys) for i in items]}),
            items=tuple(items),
            conflicts=(),
            plan_hash=canonical_hash(body),
        )

    @staticmethod
    def execution(plan: ErasurePlan) -> ExecutionResult:
        steps = tuple(
            StepResult(it.step_order, StepStatus.APPLIED, it.expected_row_count, None, 0)
            for it in sorted(plan.items, key=lambda i: i.step_order)
        )
        return ExecutionResult(ExecutionMode.LIVE, ExecutionStatus.COMMITTED, steps)

    # ------------------------------------------------------------ applying a plan (test DML)
    def apply_item(self, it: PlanItem) -> int | None:
        w, a = self.writer, it.action
        if a is PlanAction.NULLIFY_REFERENCE:
            return w.update_columns(
                it.table, it.key_columns, it.record_keys, dict.fromkeys(it.target_columns, NULL)
            )
        if a is PlanAction.ANONYMIZE_ROWS:
            return w.update_columns(
                it.table, it.key_columns, it.record_keys, self.assignments[it.step_order]
            )
        if a in (
            PlanAction.DELETE_ROWS,
            PlanAction.DELETE_DERIVED_BY_KEY,
            PlanAction.INVALIDATE_CACHE,
        ):
            return w.delete_rows(it.table, it.key_columns, it.record_keys)
        if a is PlanAction.REFRESH_MATVIEW:
            w.refresh_matview(it.table)
            return None
        if a is PlanAction.RECOMPUTE_TABLE:
            definition = self.artifact(it.table).definition_sql
            assert definition is not None
            return w.recompute_table(it.table, definition)
        return None  # EXPECT_CASCADE / RETAIN_ROWS / SCHEDULE_EXTERNAL / MANUAL_REVIEW

    def apply(self, plan: ErasurePlan, *, skip: Sequence[int] = ()) -> dict[int, int | None]:
        """Apply the plan's DML in step order and assert keyed rowcounts (like the executor)."""
        out: dict[int, int | None] = {}
        for it in sorted(plan.items, key=lambda i: i.step_order):
            if it.step_order in skip:
                continue
            n = self.apply_item(it)
            if n is not None and it.action is not PlanAction.RECOMPUTE_TABLE:
                assert n == it.expected_row_count, (it.step_order, it.action, n)
            out[it.step_order] = n
        return out

    # ------------------------------------------------------------ discovery inputs
    def rediscover(self, reader: Any) -> RecordDiscoveryResult:
        return discover_records(
            self.graph, self.root, (self.subject,), reader, DiscoveryLimits(), self.policies
        )

    def capture_probes(self) -> list[ResidualProbe]:
        """Real discovery + probe capture on the pre-erasure data (values never leave here)."""
        found = self.rediscover(self.reader)
        return capture_residual_probes(
            self.root, (self.subject,), found.record_graph, self.reader, self.policies,
            self.keyring,
        )  # fmt: skip

    def scan_columns(self) -> list[ColumnRef]:
        """Every text-like column of every target table (from the catalog, not lineage)."""
        return sorted(
            ColumnRef(ref, c.name)
            for ref, info in self.catalog.tables.items()
            for c in info.columns
            if is_text_like(c.data_type)
        )

    # ------------------------------------------------------------ verification
    def verify(
        self,
        plan: ErasurePlan,
        *,
        probes: Sequence[ResidualProbe] = (),
        scan: Sequence[ColumnRef] = (),
        rediscover: Callable[[Any], RecordDiscoveryResult] | None = None,
    ) -> VerificationReport:
        return verify_execution(
            plan,
            self.execution(plan),
            self.reader,
            rediscover or empty_rediscovery,
            self.artifacts,
            probes,
            scan,
            HMAC_KEY,
            self.policies,
        )

    # ------------------------------------------------------------ the subject's full plan
    def subject_plan(self, *, hidden_copies: bool = False, external: bool = False) -> ErasurePlan:
        """The plan an LAE run would approve for ``subject``, built from the seeded rows.

        FK-reachable rows + registered derived artifacts. ``hidden_copies`` adds the
        un-modelled marketing_event_log rows (a confirmed inferred suggestion);
        ``external`` adds the SCHEDULE_EXTERNAL item for the CRM export files.
        """
        c = self.subject
        orders = self.keys("SELECT id FROM app.purchase_order WHERE customer_id = :c", c=c)
        order_ids = [k[0] for k in orders]
        tickets = self.keys("SELECT id FROM app.support_ticket WHERE customer_id = :c", c=c)
        ticket_ids = [k[0] for k in tickets]
        docs = self.keys("SELECT id FROM app.document WHERE customer_id = :c", c=c)
        doc_ids = [k[0] for k in docs]
        steps: list[PlanItem] = []

        def add(*args: Any, **kwargs: Any) -> int:
            steps.append(self.item(len(steps) + 1, *args, **kwargs))
            return len(steps)

        add(
            PlanAction.NULLIFY_REFERENCE, CUSTOMER, ("id",),
            self.keys("SELECT id FROM app.customer WHERE referrer_id = :c", c=c), ("referrer_id",),
        )  # fmt: skip
        add(
            PlanAction.NULLIFY_REFERENCE, MESSAGE, ("id",),
            self.keys(
                "SELECT id FROM app.ticket_message WHERE author_customer_id = :c "
                "AND ticket_id <> ALL(:t)", c=c, t=ticket_ids,
            ),
            ("author_customer_id",),
        )  # fmt: skip
        add(
            PlanAction.DELETE_ROWS, MESSAGE, ("id",),
            self.keys(
                "SELECT id FROM app.ticket_message WHERE ticket_id = ANY(:t) "
                "AND (author_customer_id IS NULL OR author_customer_id = :c)", c=c, t=ticket_ids,
            ),
        )  # fmt: skip
        add(PlanAction.NULLIFY_REFERENCE, TICKET, ("id",), tickets, ("customer_id",))
        add(
            PlanAction.RETAIN_ROWS, LINE, ("order_id", "line_no"),
            self.keys(
                "SELECT order_id, line_no FROM app.order_line WHERE order_id = ANY(:o)", o=order_ids
            ),
        )  # fmt: skip
        add(
            PlanAction.RETAIN_ROWS, PAYMENT, ("id",),
            self.keys("SELECT id FROM app.payment WHERE order_id = ANY(:o)", o=order_ids),
        )  # fmt: skip
        add(
            PlanAction.RETAIN_ROWS, SHIPMENT, ("id",),
            self.keys("SELECT id FROM app.shipment WHERE order_id = ANY(:o)", o=order_ids),
        )  # fmt: skip
        anon = add(
            PlanAction.ANONYMIZE_ROWS, ORDER, ("id",), orders,
            ("shipping_address", "shipping_name"),
        )  # fmt: skip
        self.assignments[anon] = {"shipping_name": PSEUDO, "shipping_address": NULL}
        add(PlanAction.NULLIFY_REFERENCE, ORDER, ("id",), orders, ("customer_id",))
        add(
            PlanAction.DELETE_DERIVED_BY_KEY, EMBEDDING, ("id",),
            self.keys(
                "SELECT id FROM app.document_embedding WHERE source_document_id = ANY(:d)",
                d=doc_ids,
            ),
        )  # fmt: skip
        add(PlanAction.DELETE_ROWS, DOCUMENT, ("id",), docs)
        add(
            PlanAction.INVALIDATE_CACHE, CACHE, ("customer_id",),
            self.keys("SELECT customer_id FROM app.customer_profile_cache WHERE customer_id = :c",
                      c=c),
        )  # fmt: skip
        add(
            PlanAction.DELETE_ROWS, LOYALTY, ("customer_id",),
            self.keys("SELECT customer_id FROM app.loyalty_account WHERE customer_id = :c", c=c),
        )  # fmt: skip
        if hidden_copies:
            # Confirmed suggestion from the residual scan: rows addressed by their own value
            # (no primary key). The raw values exist only in the transient plan, as in record_keys.
            add(
                PlanAction.DELETE_ROWS, MARKETING, ("email",),
                self.keys(
                    "SELECT DISTINCT m.email FROM app.marketing_event_log m "
                    "JOIN app.customer s ON s.id = :c "
                    "WHERE lower(btrim(m.email, E' \\t\\n\\r\\f\\x0B')) = lower(s.email)", c=c,
                ),
                expected=self.count(
                    "SELECT count(*) FROM app.marketing_event_log m JOIN app.customer s "
                    "ON s.id = :c WHERE lower(btrim(m.email, E' \\t\\n\\r\\f\\x0B')) = "
                    "lower(s.email)", c=c,
                ),
            )  # fmt: skip
        add(PlanAction.DELETE_ROWS, CUSTOMER, ("id",), ((c,),))
        add(
            PlanAction.EXPECT_CASCADE, ADDRESS, ("id",),
            self.keys("SELECT id FROM app.customer_address WHERE customer_id = :c", c=c),
        )  # fmt: skip
        add(PlanAction.REFRESH_MATVIEW, LTV, ("customer_id",), ((c,),))
        add(PlanAction.RECOMPUTE_TABLE, SEGMENT, ("customer_id",), ((c,),))
        add(PlanAction.REFRESH_MATVIEW, REVENUE, level=LineageLevel.TABLE)
        if external:
            add(
                PlanAction.SCHEDULE_EXTERNAL, CRM_EXPORT, ("customer_id",), ((c,),),
            )  # fmt: skip
        return self.plan(steps)


@pytest.fixture
def world(seeded_db: tuple[Engine, Any], app_conn: Connection) -> World:
    _engine, truth = seeded_db
    session = Session(bind=app_conn, join_transaction_mode="create_savepoint")
    catalog = read_catalog(PgCatalogReader(app_conn), ["app"])
    policy_repo = PolicyRepo(session)
    root_rec = policy_repo.subject_root_by_name("customer")
    assert root_rec is not None
    policies = policy_repo.policy_set()
    artifacts = ArtifactRepo(session).active_definitions()
    edges = discover_foreign_keys(PgCatalogReader(app_conn), catalog)
    graph = build_lineage_graph(
        catalog,
        edges,
        {k: v for k, v in policies.pii.items() if catalog.has_table(k.table)},
        [a.target for a in artifacts if catalog.has_table(a.target)],
    )
    return World(
        conn=app_conn,
        session=session,
        catalog=catalog,
        reader=PgTargetReader(app_conn, catalog),
        writer=PgTargetWriter(app_conn, catalog, hmac_key=HMAC_KEY),
        truth=truth,
        policies=policies,
        artifacts=artifacts,
        root=root_rec.root,
        graph=graph,
        keyring=HmacKeyring(HMAC_KEY),
        subject=int(truth.meta["known_subjects"]["primary"]),
    )
