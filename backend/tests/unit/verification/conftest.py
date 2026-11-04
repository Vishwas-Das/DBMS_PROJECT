"""In-memory fakes for lae.verification unit tests (no database).

`FakeReader` implements the `TargetReader` port over dict rows. `hmac_match_count` uses the
Python `normalize()`; the SQL twin is covered by the DB adapter's integration tests.

`make_world()` builds the post-erasure state of a small shop after a *correct* LIVE erasure of
subject customer 4242, together with the plan that produced it.
"""

from __future__ import annotations

import copy
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import ModuleType
from typing import Any
from uuid import UUID, uuid5

import pytest

from lae.audit.hmac_keys import HmacKeyring
from lae.contracts.enums import (
    ArtifactKind,
    ExecutionMode,
    ExecutionStatus,
    LineageLevel,
    Normalization,
    PlanAction,
    RecordRole,
    RefreshStrategy,
    RetentionAction,
    StepStatus,
)
from lae.contracts.types import (
    ArtifactDefinition,
    ColumnRef,
    ErasurePlan,
    ExecutionResult,
    KeyMapping,
    KeyTuple,
    PlanItem,
    PolicySet,
    RecordKey,
    ResidualProbe,
    StepResult,
    TableRef,
    VerificationReport,
)
from lae.errors import SubjectNotFoundError, TargetQueryError
from lae.lineage.record_graph import RecordGraph
from lae.lineage.results import RecordDiscoveryResult
from lae.verification import verify_execution

HMAC_KEY = bytes(range(32, 64))
NS = UUID("00000000-0000-0000-0000-0000000000ee")
SUBJECT = 4242
OTHER = 7
SUBJECT_EMAIL = "alice@example.com"
SUBJECT_NAME = "Alice Smith"
PII_STRINGS = (SUBJECT_EMAIL, "Alice", "alice", SUBJECT_NAME, "4242")


def t(name: str) -> TableRef:
    return TableRef("app", name)


CUSTOMER = t("customer")
ORDER = t("purchase_order")
INVOICE = t("invoice")
LEDGER = t("ledger")
REFERRAL = t("referral")
DOCUMENT = t("document")
EMBEDDING = t("document_embedding")
CACHE = t("customer_profile_cache")
LTV = t("customer_ltv")
MARKETING = t("marketing_event_log")


class FakeReader:
    """TargetReader over in-memory rows. ``fail`` names methods that raise TargetQueryError."""

    def __init__(self, tables: Mapping[TableRef, list[dict[str, Any]]]) -> None:
        self.tables: dict[TableRef, list[dict[str, Any]]] = {
            k: [dict(r) for r in v] for k, v in tables.items()
        }
        self.symdiff: dict[TableRef, int] = {}
        self.fail: set[str] = set()
        self.calls: list[str] = []
        self.count_override: Any = None

    def _rows(self, table: TableRef) -> list[dict[str, Any]]:
        if table not in self.tables:
            raise TargetQueryError("unknown table", details={"table": table.qualified()})
        return self.tables[table]

    def _guard(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            # message deliberately contains a PII value: it must never surface
            raise TargetQueryError(f"boom DETAIL: Key (email)=({SUBJECT_EMAIL})")

    def _match(
        self, table: TableRef, columns: Sequence[str], values: Sequence[KeyTuple]
    ) -> list[dict[str, Any]]:
        wanted = {tuple(v) for v in values}
        return [r for r in self._rows(table) if tuple(r[c] for c in columns) in wanted]

    def select_by_columns(
        self,
        table: TableRef,
        columns: Sequence[str],
        values: Sequence[KeyTuple],
        select: Sequence[str],
    ) -> list[dict[str, Any]]:
        self._guard("select_by_columns")
        return [{c: r[c] for c in select if c in r} for r in self._match(table, columns, values)]

    def count_by_columns(
        self, table: TableRef, columns: Sequence[str], values: Sequence[KeyTuple]
    ) -> int:
        self._guard("count_by_columns")
        if self.count_override is not None:
            return self.count_override  # type: ignore[no-any-return]
        return len(self._match(table, columns, values))

    def fetch_columns(
        self, table: TableRef, key_columns: Sequence[str], key: KeyTuple, columns: Sequence[str]
    ) -> dict[str, Any] | None:  # pragma: no cover - not used by verification
        rows = self._match(table, key_columns, [key])
        return {c: rows[0][c] for c in columns} if rows else None

    def run_readonly_sql(
        self, sql: str, params: Mapping[str, Any]
    ) -> list[tuple[Any, ...]]:  # pragma: no cover - not used by verification
        raise NotImplementedError

    def hmac_match_count(
        self,
        col: ColumnRef,
        digests: Sequence[str],
        normalization: Normalization,
        hmac_key: bytes,
    ) -> int:
        self._guard("hmac_match_count")
        ring = HmacKeyring(hmac_key)
        wanted = set(digests)
        return sum(
            1
            for r in self._rows(col.table)
            if isinstance(r.get(col.column), str)
            and ring.digest(r[col.column], normalization) in wanted
        )

    def symmetric_difference_count(self, stored: TableRef, definition_sql: str) -> int:
        self._guard("symmetric_difference_count")
        self._rows(stored)
        return self.symdiff.get(stored, 0)


def item(
    step: int,
    action: PlanAction,
    table: TableRef,
    key_columns: tuple[str, ...] = ("id",),
    keys: tuple[KeyTuple, ...] = (),
    target_columns: tuple[str, ...] = (),
    level: LineageLevel = LineageLevel.RECORD,
) -> PlanItem:
    return PlanItem(
        step_order=step,
        action=action,
        table=table,
        lineage_level=level,
        key_columns=key_columns if level is LineageLevel.RECORD else (),
        record_keys=keys,
        target_columns=target_columns,
        expected_row_count=len(keys) if level is LineageLevel.RECORD else None,
    )


def make_plan(items: Sequence[PlanItem]) -> ErasurePlan:
    return ErasurePlan(
        plan_id=uuid5(NS, "plan"),
        request_id=uuid5(NS, "request"),
        schema_fingerprint="a" * 64,
        record_set_hash="b" * 64,
        items=tuple(items),
        conflicts=(),
        plan_hash="c" * 64,
    )


def default_items() -> list[PlanItem]:
    P = PlanAction  # noqa: N806
    return [
        item(1, P.NULLIFY_REFERENCE, REFERRAL, keys=((500,),), target_columns=("referrer_id",)),
        item(2, P.ANONYMIZE_ROWS, INVOICE, keys=((300,),), target_columns=("email", "name")),
        item(3, P.RETAIN_ROWS, LEDGER, keys=((400,),)),
        item(4, P.DELETE_DERIVED_BY_KEY, EMBEDDING, keys=((700,),)),
        item(5, P.INVALIDATE_CACHE, CACHE, key_columns=("customer_id",), keys=((SUBJECT,),)),
        item(6, P.EXPECT_CASCADE, ORDER, keys=((100,), (101,))),
        item(7, P.EXPECT_CASCADE, DOCUMENT, keys=((600,),)),
        item(8, P.DELETE_ROWS, CUSTOMER, keys=((SUBJECT,),)),
        item(9, P.REFRESH_MATVIEW, LTV, level=LineageLevel.TABLE),
    ]


def post_erasure_tables() -> dict[TableRef, list[dict[str, Any]]]:
    """State after a correct erasure of SUBJECT."""
    return {
        CUSTOMER: [{"id": OTHER, "email": "bob@example.com", "name": "Bob"}],
        ORDER: [{"id": 200, "customer_id": OTHER}],
        INVOICE: [
            {"id": 300, "customer_id": SUBJECT, "email": "anon_0123456789abcdef", "name": None},
            {"id": 301, "customer_id": OTHER, "email": "bob@example.com", "name": "Bob"},
        ],
        # legal hold: retained untouched, still holds the subject's email
        LEDGER: [{"id": 400, "customer_id": SUBJECT, "email": SUBJECT_EMAIL}],
        REFERRAL: [{"id": 500, "customer_id": OTHER, "referrer_id": None}],
        DOCUMENT: [{"id": 601, "customer_id": OTHER, "body": "Bob's note"}],
        EMBEDDING: [{"id": 701, "source_document_id": 601}],
        CACHE: [{"customer_id": OTHER, "payload": "{}"}],
        LTV: [{"customer_id": OTHER, "ltv": 10}],
        MARKETING: [{"id": 900, "email": "carol@example.com"}],
    }


def default_artifacts() -> list[ArtifactDefinition]:
    return [
        ArtifactDefinition(
            uuid5(NS, "ltv"),
            "customer_ltv",
            ArtifactKind.MATERIALIZED_VIEW,
            LTV,
            "SELECT customer_id, sum(total) AS ltv FROM app.purchase_order GROUP BY 1",
            RefreshStrategy.REFRESH_MATVIEW,
            (KeyMapping(CUSTOMER, ("id",), ("customer_id",)),),
        ),
        ArtifactDefinition(
            uuid5(NS, "emb"),
            "document_embedding",
            ArtifactKind.EMBEDDING_TABLE,
            EMBEDDING,
            None,
            RefreshStrategy.DELETE_BY_KEY,
            (KeyMapping(DOCUMENT, ("id",), ("source_document_id",)),),
        ),
        ArtifactDefinition(
            uuid5(NS, "cache"),
            "customer_profile_cache",
            ArtifactKind.CACHE,
            CACHE,
            None,
            RefreshStrategy.INVALIDATE_BY_KEY,
            (KeyMapping(CUSTOMER, ("id",), ("customer_id",)),),
        ),
    ]


def default_probes() -> list[ResidualProbe]:
    ring = HmacKeyring(HMAC_KEY)
    return [
        ResidualProbe(
            ColumnRef(CUSTOMER, "email"),
            Normalization.LOWER_TRIM,
            ring.digest(SUBJECT_EMAIL, Normalization.LOWER_TRIM),
        ),
        ResidualProbe(ColumnRef(CUSTOMER, "name"), Normalization.EXACT, ring.digest(SUBJECT_NAME)),
    ]


SCAN_COLUMNS = [
    ColumnRef(CUSTOMER, "email"),
    ColumnRef(CUSTOMER, "name"),
    ColumnRef(INVOICE, "email"),
    ColumnRef(INVOICE, "name"),
    ColumnRef(LEDGER, "email"),
    ColumnRef(DOCUMENT, "body"),
    ColumnRef(MARKETING, "email"),
]

POLICIES = PolicySet(retention={INVOICE: RetentionAction.ANONYMIZE, LEDGER: RetentionAction.RETAIN})


def walk_rediscover(reader: FakeReader) -> RecordDiscoveryResult:
    """Fake record discovery: subject root + rows with customer_id = SUBJECT (OWNED)."""
    rows = reader.tables[CUSTOMER]
    if not any(r["id"] == SUBJECT for r in rows):
        raise SubjectNotFoundError("subject not found")
    g = RecordGraph()
    root = RecordKey(CUSTOMER, ("id",), (SUBJECT,))
    g.add_root(root)
    for table in (ORDER, INVOICE, LEDGER, DOCUMENT):
        for r in reader.tables[table]:
            if r.get("customer_id") == SUBJECT:
                g.add_dependency(
                    root,
                    RecordKey(table, ("id",), (r["id"],)),
                    uuid5(NS, table.name),
                    RecordRole.OWNED,
                )
    return RecordDiscoveryResult(g, {})


def committed() -> ExecutionResult:
    return ExecutionResult(
        ExecutionMode.LIVE,
        ExecutionStatus.COMMITTED,
        (StepResult(1, StepStatus.APPLIED, 1, 1, 3),),
    )


@dataclass
class World:
    reader: FakeReader
    items: list[PlanItem] = field(default_factory=default_items)
    artifacts: list[ArtifactDefinition] = field(default_factory=default_artifacts)
    probes: list[ResidualProbe] = field(default_factory=default_probes)
    scan_columns: list[ColumnRef] = field(default_factory=lambda: list(SCAN_COLUMNS))
    policies: PolicySet = POLICIES
    execution: ExecutionResult = field(default_factory=committed)
    hmac_key: bytes = HMAC_KEY
    rediscover: Callable[[Any], RecordDiscoveryResult] = walk_rediscover

    @property
    def plan(self) -> ErasurePlan:
        return make_plan(self.items)

    def rows(self, table: TableRef) -> list[dict[str, Any]]:
        return self.reader.tables[table]

    def verify(self) -> VerificationReport:
        return verify_execution(
            self.plan,
            self.execution,
            self.reader,
            self.rediscover,
            self.artifacts,
            self.probes,
            self.scan_columns,
            self.hmac_key,
            self.policies,
        )

    def replace_item(self, step: int, **changes: Any) -> None:
        self.items = [replace(i, **changes) if i.step_order == step else i for i in self.items]


@pytest.fixture
def world() -> World:
    return World(FakeReader(copy.deepcopy(post_erasure_tables())))


@pytest.fixture
def ring() -> HmacKeyring:
    return HmacKeyring(HMAC_KEY)


@pytest.fixture(scope="session")
def h() -> ModuleType:
    """This module: gives tests access to the helpers and constants above."""
    return sys.modules[__name__]
