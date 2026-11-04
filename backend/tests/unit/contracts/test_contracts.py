from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from lae.config import Settings
from lae.contracts.enums import (
    EdgeOrigin,
    EdgeStatus,
    LineageLevel,
    PlanAction,
    Severity,
    TableKind,
    Traceability,
    Transform,
)
from lae.contracts.ports import AuditSink, CatalogReader, TargetReader, TargetWriter
from lae.contracts.types import (
    CatalogSnapshot,
    ColumnInfo,
    ColumnRef,
    ErasurePlan,
    PlanConflict,
    PlanItem,
    RecordKey,
    RelationshipEdge,
    TableInfo,
    TableRef,
)
from lae.errors import (
    ConfigurationError,
    LaeError,
    RowCountMismatch,
    UnknownColumnError,
    UnknownTableError,
    ValidationError,
)

pytestmark = pytest.mark.unit

CUSTOMER = TableRef("app", "customer")


def _catalog() -> CatalogSnapshot:
    info = TableInfo(
        ref=CUSTOMER,
        kind=TableKind.TABLE,
        columns=(ColumnInfo("id", 1, "bigint", False), ColumnInfo("email", 2, "text", False)),
        primary_key=("id",),
    )
    return CatalogSnapshot(tables={CUSTOMER: info}, schemas=("app",), fingerprint="0" * 64)


def test_tableref_parse_roundtrip() -> None:
    assert TableRef.parse("app.customer") == CUSTOMER
    assert CUSTOMER.qualified() == "app.customer"
    with pytest.raises(ValidationError):
        TableRef.parse("customer")


def test_catalog_lookups_raise_typed_errors() -> None:
    cat = _catalog()
    assert cat.column(ColumnRef(CUSTOMER, "email")).data_type == "text"
    with pytest.raises(UnknownTableError):
        cat.table(TableRef("app", "nope"))
    with pytest.raises(UnknownColumnError):
        cat.column(ColumnRef(CUSTOMER, "nope"))
    with pytest.raises(TypeError):
        cat.tables[TableRef("app", "x")] = cat.table(CUSTOMER)  # type: ignore[index]


def test_record_key_validation_and_sorting() -> None:
    a = RecordKey(CUSTOMER, ("id",), (2,))
    b = RecordKey(CUSTOMER, ("id",), (10,))
    c = RecordKey(CUSTOMER, ("id",), ("x",))
    assert sorted([c, b, a], key=RecordKey.sort_key) == [a, b, c]
    assert hash(a) == hash(RecordKey(CUSTOMER, ("id",), (2,)))
    with pytest.raises(ValidationError):
        RecordKey(CUSTOMER, ("id",), ())
    with pytest.raises(ValidationError):
        RecordKey(CUSTOMER, ("id",), (True,))


def test_relationship_edge_invariants() -> None:
    kw = dict(
        edge_id=uuid4(),
        origin=EdgeOrigin.DECLARED_FK,
        source=CUSTOMER,
        target=TableRef("app", "purchase_order"),
        transform=Transform.FOREIGN_KEY,
        traceability=Traceability.KEY_PRESERVING,
        status=EdgeStatus.CONFIRMED,
    )
    RelationshipEdge(
        level=LineageLevel.TABLE, source_columns=("id",), target_columns=("customer_id",), **kw
    )  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        RelationshipEdge(level=LineageLevel.TABLE, source_columns=("id",), target_columns=(), **kw)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        RelationshipEdge(level=LineageLevel.RECORD, source_columns=(), target_columns=(), **kw)  # type: ignore[arg-type]


def test_plan_blocked_only_by_unresolved_blocking_conflicts() -> None:
    item = PlanItem(
        1,
        PlanAction.DELETE_ROWS,
        CUSTOMER,
        LineageLevel.RECORD,
        ("id",),
        ((42,),),
        expected_row_count=1,
    )

    def plan(*conflicts: PlanConflict) -> ErasurePlan:
        return ErasurePlan(uuid4(), uuid4(), "0" * 64, "1" * 64, (item,), conflicts, "2" * 64)

    from lae.contracts.enums import ConflictCode

    warn = PlanConflict(ConflictCode.RETAINED_WITH_PII, Severity.WARNING, CUSTOMER, "w")
    block = PlanConflict(ConflictCode.SHARED_RECORD, Severity.BLOCKING, CUSTOMER, "b")
    resolved = PlanConflict(ConflictCode.SHARED_RECORD, Severity.BLOCKING, CUSTOMER, "b", True)
    assert not plan().is_blocked
    assert not plan(warn, resolved).is_blocked
    assert plan(block).is_blocked
    with pytest.raises(ValidationError):
        PlanItem(1, PlanAction.DELETE_ROWS, CUSTOMER, LineageLevel.RECORD, ("id",), ((42,),))


def test_errors_carry_codes_without_values() -> None:
    e = RowCountMismatch(3, expected=5, actual=4)
    assert isinstance(e, LaeError) and e.code == "ROW_COUNT_MISMATCH"
    assert e.details == {"step_order": 3, "expected": 5, "actual": 4}


def test_settings_reject_engine_schema_and_short_hmac_key() -> None:
    with pytest.raises(ValueError):
        Settings(target_schemas=["app", "lae"])
    assert Settings(target_schemas="app, crm").target_schemas == ["app", "crm"]  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError):
        Settings(hmac_key="short").hmac_key_bytes()  # type: ignore[arg-type]
    assert len(Settings(hmac_key="k" * 32).hmac_key_bytes()) == 32  # type: ignore[arg-type]


def test_ports_are_runtime_checkable_protocols() -> None:
    for proto in (CatalogReader, TargetReader, TargetWriter, AuditSink):
        assert not isinstance(object(), proto)
    _ = datetime.now(UTC)


def test_every_plan_action_has_a_category() -> None:
    from lae.contracts.enums import ActionCategory, action_category

    cats = {action_category(a) for a in PlanAction}
    assert cats == set(ActionCategory)
    assert action_category(PlanAction.REFRESH_MATVIEW) is ActionCategory.RECOMPUTE
    assert action_category(PlanAction.ANONYMIZE_ROWS) is ActionCategory.RETAIN
