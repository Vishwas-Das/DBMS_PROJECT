"""verify_execution: every check passing and failing, outcome/coverage, errors, no PII."""

from __future__ import annotations

import json
from dataclasses import replace
from fractions import Fraction
from types import ModuleType
from typing import Any

import pytest

from lae.audit.canonical import canonical_json
from lae.contracts.enums import (
    CheckType,
    ExecutionMode,
    ExecutionStatus,
    LineageLevel,
    PlanAction,
    RetentionAction,
    VerificationOutcome,
    VerificationStatus,
)
from lae.contracts.types import (
    CheckResult,
    ColumnRef,
    PolicySet,
    ResidualProbe,
    VerificationReport,
)
from lae.errors import VerificationError

pytestmark = pytest.mark.unit

CT = CheckType


def find(report: VerificationReport, ct: CheckType, step: int | None = None) -> list[CheckResult]:
    return [c for c in report.checks if c.check_type is ct and c.step_order == step]


def one(report: VerificationReport, ct: CheckType, step: int | None = None) -> CheckResult:
    found = find(report, ct, step)
    assert len(found) == 1, (ct, step, report.checks)
    return found[0]


def failed(report: VerificationReport) -> set[tuple[CheckType, int | None]]:
    return {(c.check_type, c.step_order) for c in report.checks if not c.passed}


# ------------------------------------------------------------------ the happy path


def test_correct_erasure_is_verified(world: Any) -> None:
    r = world.verify()
    assert failed(r) == set()
    assert r.status is VerificationStatus.PASSED
    assert r.outcome is VerificationOutcome.VERIFIED
    assert (r.required_artifacts, r.verified_artifacts) == (9, 9)
    assert r.coverage == Fraction(1)
    expected = {
        (CT.REFERENCE_NULLIFIED, 1),
        (CT.COLUMNS_ANONYMIZED, 2),
        (CT.RETENTION_HONORED, 3),
        (CT.ROW_ABSENT, 4),
        (CT.EMBEDDING_ABSENT, 4),
        (CT.ROW_ABSENT, 5),
        (CT.CASCADE_COMPLETE, 6),
        (CT.CASCADE_COMPLETE, 7),
        (CT.ROW_ABSENT, 8),
        (CT.DERIVED_CONSISTENT, 9),
        (CT.ROW_ABSENT, 9),
        (CT.REDISCOVERY_EMPTY, None),
        (CT.RESIDUAL_VALUE_SCAN, None),
    }
    assert {(c.check_type, c.step_order) for c in r.checks} == expected
    assert [c.column for c in find(r, CT.COLUMNS_ANONYMIZED, 2)] == ["email", "name"]


def test_checks_sorted_and_deterministic(world: Any) -> None:
    a, b = world.verify(), world.verify()
    assert a.checks == b.checks
    steps = [c.step_order for c in a.checks]
    numbered = [s for s in steps if s is not None]
    assert numbered == sorted(numbered)
    assert steps[-2:] == [None, None]


def test_item_order_in_plan_does_not_matter(world: Any) -> None:
    a = world.verify()
    world.items = list(reversed(world.items))
    assert world.verify().checks == a.checks


def test_observed_expected_hold_only_counts(world: Any, h: ModuleType) -> None:
    # Make several checks fail so that failure payloads are covered too.
    world.rows(h.MARKETING).append({"id": 901, "email": " Alice@Example.com\t"})
    world.rows(h.CUSTOMER).append({"id": h.SUBJECT, "email": h.SUBJECT_EMAIL, "name": "Alice"})
    world.rows(h.INVOICE)[0]["email"] = h.SUBJECT_EMAIL
    world.reader.symdiff[h.LTV] = 2
    r = world.verify()
    assert r.status is VerificationStatus.FAILED
    text = canonical_json(
        [{"o": dict(c.observed), "e": dict(c.expected)} for c in r.checks]
    ).decode()
    for pii in h.PII_STRINGS:
        assert pii not in text

    def leaf_ok(v: object) -> bool:
        if isinstance(v, bool | int) or v is None:
            return True
        if isinstance(v, list):
            return all(
                isinstance(d, dict) and set(d) in ({"table", "column", "count"}, {"table", "count"})
                for d in v
            )
        return False

    for c in r.checks:
        for v in (*c.observed.values(), *c.expected.values()):
            assert leaf_ok(v), (c.check_type, v)


# ------------------------------------------------------------------ deletion checks


def test_row_absent_fails_when_root_remains(world: Any, h: ModuleType) -> None:
    world.rows(h.CUSTOMER).append({"id": h.SUBJECT, "email": "x", "name": "y"})
    r = world.verify()
    c = one(r, CT.ROW_ABSENT, 8)
    assert not c.passed
    assert dict(c.observed) == {"remaining_rows": 1}
    assert dict(c.expected) == {"remaining_rows": 0, "planned_keys": 1}
    assert (CT.REDISCOVERY_EMPTY, None) in failed(r)  # root rediscovered
    assert r.outcome is VerificationOutcome.FAILED
    assert r.status is VerificationStatus.FAILED
    assert r.verified_artifacts == 8


def test_cascade_complete_fails(world: Any, h: ModuleType) -> None:
    world.rows(h.ORDER).append({"id": 101, "customer_id": h.SUBJECT})
    r = world.verify()
    c = one(r, CT.CASCADE_COMPLETE, 6)
    assert not c.passed and c.observed["remaining_rows"] == 1
    assert c.expected["planned_keys"] == 2


def test_invalidate_cache_fails(world: Any, h: ModuleType) -> None:
    world.rows(h.CACHE).append({"customer_id": h.SUBJECT, "payload": "{}"})
    assert failed(world.verify()) == {(CT.ROW_ABSENT, 5)}


def test_derived_by_key_and_embedding_fail_together(world: Any, h: ModuleType) -> None:
    world.rows(h.EMBEDDING).append({"id": 700, "source_document_id": 600})
    r = world.verify()
    assert failed(r) == {(CT.ROW_ABSENT, 4), (CT.EMBEDDING_ABSENT, 4)}
    e = one(r, CT.EMBEDDING_ABSENT, 4)
    assert dict(e.observed) == {"remaining_rows": 1}
    assert dict(e.expected) == {"remaining_rows": 0, "source_keys": 1}


def test_embedding_absent_runs_without_a_plan_item(world: Any, h: ModuleType) -> None:
    """The planner forgot the embedding table: EMBEDDING_ABSENT still catches the vector."""
    world.items = [i for i in world.items if i.step_order != 4]
    world.rows(h.EMBEDDING).append({"id": 700, "source_document_id": 600})
    r = world.verify()
    assert failed(r) == {(CT.EMBEDDING_ABSENT, None)}
    assert one(r, CT.EMBEDDING_ABSENT, None).table == h.EMBEDDING
    assert r.outcome is VerificationOutcome.FAILED


def test_embedding_check_skipped_without_source_keys(world: Any) -> None:
    world.items = [i for i in world.items if i.step_order not in (4, 7)]
    assert find(world.verify(), CT.EMBEDDING_ABSENT, None) == []


def test_empty_record_item_passes_vacuously_without_reader_call(world: Any, h: ModuleType) -> None:
    from lae.contracts.types import PlanItem

    world.items = [
        PlanItem(1, PlanAction.DELETE_ROWS, h.ORDER, LineageLevel.RECORD, ("id",), (), (), 0)
    ]
    world.reader.calls.clear()
    r = world.verify()
    assert one(r, CT.ROW_ABSENT, 1).passed
    assert "count_by_columns" not in world.reader.calls


# ------------------------------------------------------------------ retained rows


def test_columns_anonymized_fails_on_raw_value(world: Any, h: ModuleType) -> None:
    world.rows(h.INVOICE)[0]["email"] = h.SUBJECT_EMAIL
    r = world.verify()
    email, name = find(r, CT.COLUMNS_ANONYMIZED, 2)
    assert (email.column, email.passed) == ("email", False)
    assert dict(email.observed) == {"rows_present": 1, "violations": 1}
    assert name.passed
    assert r.verified_artifacts == 8


@pytest.mark.parametrize(
    "value",
    [
        "anon_0123456789ABCDEF",  # uppercase hex
        "anon_0123456789abcde",  # 15 hex
        "anon_0123456789abcdef0",  # 17 hex
        "anon_0123456789abcdef\n",  # trailing newline ($ would accept it)
        " anon_0123456789abcdef",
        "",
        12345,
    ],
)
def test_pseudonym_pattern_is_strict(world: Any, h: ModuleType, value: object) -> None:
    world.rows(h.INVOICE)[0]["email"] = value
    assert (CT.COLUMNS_ANONYMIZED, 2) in failed(world.verify())


def test_anonymized_row_deleted_fails(world: Any, h: ModuleType) -> None:
    del world.rows(h.INVOICE)[0]
    for c in find(world.verify(), CT.COLUMNS_ANONYMIZED, 2):
        assert not c.passed and c.observed["rows_present"] == 0


def test_anonymize_without_target_columns_checks_presence(world: Any, h: ModuleType) -> None:
    world.replace_item(2, target_columns=())
    (c,) = find(world.verify(), CT.COLUMNS_ANONYMIZED, 2)
    assert c.column is None and c.passed
    del world.rows(h.INVOICE)[0]
    (c,) = find(world.verify(), CT.COLUMNS_ANONYMIZED, 2)
    assert not c.passed


def test_retention_honored_fails_when_row_deleted(world: Any, h: ModuleType) -> None:
    world.rows(h.LEDGER).clear()
    r = world.verify()
    c = one(r, CT.RETENTION_HONORED, 3)
    assert not c.passed
    assert (dict(c.observed), dict(c.expected)) == ({"rows_present": 0}, {"rows_present": 1})


def test_retention_honored_fails_on_extra_row(world: Any, h: ModuleType) -> None:
    world.rows(h.LEDGER).append(dict(world.rows(h.LEDGER)[0]))  # duplicate key => count 2
    assert (CT.RETENTION_HONORED, 3) in failed(world.verify())


# ------------------------------------------------------------------ references


def test_reference_not_nullified(world: Any, h: ModuleType) -> None:
    world.rows(h.REFERRAL)[0]["referrer_id"] = h.SUBJECT
    c = one(world.verify(), CT.REFERENCE_NULLIFIED, 1)
    assert not c.passed and c.observed["violations"] == 1


def test_referencing_row_of_other_subject_vanished(world: Any, h: ModuleType) -> None:
    world.rows(h.REFERRAL).clear()
    c = one(world.verify(), CT.REFERENCE_NULLIFIED, 1)
    assert not c.passed
    assert c.observed["rows_missing"] == 1 and c.observed["rows_deleted_by_plan"] == 0


def test_nullified_then_deleted_row_is_fine(world: Any, h: ModuleType) -> None:
    """A cycle-breaking NULLIFY on a row that a later DELETE removes."""
    world.items.append(h.item(10, PlanAction.DELETE_ROWS, h.REFERRAL, keys=((500,),)))
    world.rows(h.REFERRAL).clear()
    r = world.verify()
    c = one(r, CT.REFERENCE_NULLIFIED, 1)
    assert c.passed and c.observed["rows_deleted_by_plan"] == 1
    assert r.outcome is VerificationOutcome.VERIFIED


def test_nullify_without_target_columns(world: Any) -> None:
    world.replace_item(1, target_columns=())
    (c,) = find(world.verify(), CT.REFERENCE_NULLIFIED, 1)
    assert c.column is None and c.passed


# ------------------------------------------------------------------ derived artifacts


def test_derived_inconsistent(world: Any, h: ModuleType) -> None:
    world.reader.symdiff[h.LTV] = 3
    r = world.verify()
    c = one(r, CT.DERIVED_CONSISTENT, 9)
    assert not c.passed and dict(c.observed) == {"difference_rows": 3}
    assert one(r, CT.ROW_ABSENT, 9).passed


def test_subject_row_left_in_key_preserving_artifact(world: Any, h: ModuleType) -> None:
    world.rows(h.LTV).append({"customer_id": h.SUBJECT, "ltv": 99})
    r = world.verify()
    assert failed(r) == {(CT.ROW_ABSENT, 9)}
    assert one(r, CT.DERIVED_CONSISTENT, 9).passed


def test_record_level_refresh_uses_item_keys(world: Any, h: ModuleType) -> None:
    world.replace_item(
        9,
        lineage_level=LineageLevel.RECORD,
        key_columns=("customer_id",),
        record_keys=((h.SUBJECT,),),
        expected_row_count=1,
    )
    world.rows(h.LTV).append({"customer_id": h.SUBJECT, "ltv": 99})
    assert failed(world.verify()) == {(CT.ROW_ABSENT, 9)}


def test_aggregate_without_key_mapping_has_no_row_check(world: Any, h: ModuleType) -> None:
    world.artifacts = [
        replace(a, key_mappings=()) if a.target == h.LTV else a for a in world.artifacts
    ]
    r = world.verify()
    assert find(r, CT.ROW_ABSENT, 9) == []
    assert one(r, CT.DERIVED_CONSISTENT, 9).passed


def test_mapping_on_non_key_column_is_ignored(world: Any, h: ModuleType) -> None:
    from lae.contracts.types import KeyMapping

    world.artifacts = [
        replace(a, key_mappings=(KeyMapping(h.CUSTOMER, ("email",), ("customer_id",)),))
        if a.target == h.LTV
        else a
        for a in world.artifacts
    ]
    assert find(world.verify(), CT.ROW_ABSENT, 9) == []


def test_recompute_table_is_checked(world: Any) -> None:
    world.replace_item(9, action=PlanAction.RECOMPUTE_TABLE)
    assert one(world.verify(), CT.DERIVED_CONSISTENT, 9).passed


# ------------------------------------------------------------------ rediscovery


def test_rediscovery_finds_leftover_owned_row(world: Any, h: ModuleType) -> None:
    """Lineage knows the table, but the plan missed one of the rows."""
    from lae.contracts.enums import RecordRole
    from lae.contracts.types import RecordKey
    from lae.lineage.record_graph import RecordGraph
    from lae.lineage.results import RecordDiscoveryResult

    def rediscover(_reader: object) -> RecordDiscoveryResult:
        g = RecordGraph()
        root = RecordKey(h.INVOICE, ("id",), (300,))  # retained (ANONYMIZE item)
        g.add_root(root)
        g.add_dependency(root, RecordKey(h.LEDGER, ("id",), (400,)), h.NS, RecordRole.OWNED)
        g.add_dependency(root, RecordKey(h.ORDER, ("id",), (102,)), h.NS, RecordRole.OWNED)
        g.add_dependency(root, RecordKey(h.ORDER, ("id",), (103,)), h.NS, RecordRole.OWNED)
        g.add_dependency(root, RecordKey(h.DOCUMENT, ("id",), (9,)), h.NS, RecordRole.SHARED)
        g.add_dependency(
            root, RecordKey(h.REFERRAL, ("id",), (8,)), h.NS, RecordRole.REFERENCING_OTHER_SUBJECT
        )
        return RecordDiscoveryResult(g, {})

    world.rediscover = rediscover
    r = world.verify()
    c = one(r, CT.REDISCOVERY_EMPTY)
    assert not c.passed
    assert dict(c.observed) == {
        "remaining_records": 2,
        "retained_records": 2,
        "subject_root_found": True,
        "remaining_by_table": [{"table": "app.purchase_order", "count": 2}],
    }
    assert r.outcome is VerificationOutcome.FAILED
    assert r.verified_artifacts == 9  # global failure does not unverify items ...
    assert r.status is VerificationStatus.FAILED  # ... but the run fails


def test_rediscovery_with_retained_root_passes(world: Any, h: ModuleType) -> None:
    from lae.contracts.types import RecordKey
    from lae.lineage.record_graph import RecordGraph
    from lae.lineage.results import RecordDiscoveryResult

    def rediscover(_reader: object) -> RecordDiscoveryResult:
        g = RecordGraph()
        g.add_root(RecordKey(h.LEDGER, ("id",), (400,)))
        return RecordDiscoveryResult(g, {})

    world.rediscover = rediscover
    c = one(world.verify(), CT.REDISCOVERY_EMPTY)
    assert c.passed and c.observed["retained_records"] == 1


def test_rediscovery_subject_gone(world: Any) -> None:
    c = one(world.verify(), CT.REDISCOVERY_EMPTY)
    assert c.passed and c.observed["subject_root_found"] is False


def test_rediscovery_error_is_verification_error(world: Any, h: ModuleType) -> None:
    def broken(_reader: object) -> Any:
        raise RuntimeError(h.SUBJECT_EMAIL)

    world.rediscover = broken
    with pytest.raises(VerificationError) as ei:
        world.verify()
    assert ei.value.details["check_type"] == "REDISCOVERY_EMPTY"
    assert ei.value.details["cause"] == "RuntimeError"
    assert h.SUBJECT_EMAIL not in repr(ei.value) + str(ei.value.details)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__


# ------------------------------------------------------------------ residual scan


def test_residual_scan_catches_hidden_copy_lineage_missed(world: Any, h: ModuleType) -> None:
    """marketing_event_log has no lineage edge and no plan item: only the scan sees it."""
    world.rows(h.MARKETING).append({"id": 901, "email": "  ALICE@example.COM\t"})
    world.rows(h.DOCUMENT).append({"id": 602, "customer_id": None, "body": h.SUBJECT_NAME})
    r = world.verify()
    assert failed(r) == {(CT.RESIDUAL_VALUE_SCAN, None)}
    c = one(r, CT.RESIDUAL_VALUE_SCAN)
    assert c.observed["hits"] == [
        {"table": "app.document", "column": "body", "count": 1},
        {"table": "app.marketing_event_log", "column": "email", "count": 1},
    ]
    assert c.observed["total_hits"] == 2
    assert c.observed["retained_hits"] == [{"table": "app.ledger", "column": "email", "count": 1}]
    assert dict(c.expected) == {"total_hits": 0}
    assert (r.outcome, r.status) == (VerificationOutcome.FAILED, VerificationStatus.FAILED)
    assert r.coverage == 1  # every item verified, yet the residual failure fails the run
    assert h.SUBJECT_EMAIL not in json.dumps(dict(c.observed))


def test_residual_counts_sum_over_normalizations(world: Any, h: ModuleType, ring: Any) -> None:
    from lae.contracts.enums import Normalization

    world.rows(h.MARKETING).append({"id": 901, "email": h.SUBJECT_EMAIL})
    world.probes.append(
        ResidualProbe(
            ColumnRef(h.CUSTOMER, "email"), Normalization.EXACT, ring.digest(h.SUBJECT_EMAIL)
        )
    )
    world.probes.append(world.probes[0])  # duplicate probe is de-duplicated
    c = one(world.verify(), CT.RESIDUAL_VALUE_SCAN)
    assert c.observed["hits"] == [
        {"table": "app.marketing_event_log", "column": "email", "count": 2}
    ]
    assert c.observed["probe_count"] == 3


def test_retained_table_without_retain_item_counts_as_hit(world: Any, h: ModuleType) -> None:
    world.items = [i for i in world.items if i.step_order != 3]
    c = one(world.verify(), CT.RESIDUAL_VALUE_SCAN)
    assert not c.passed
    assert c.observed["hits"] == [{"table": "app.ledger", "column": "email", "count": 1}]


def test_retain_item_without_retain_policy_counts_as_hit(world: Any, h: ModuleType) -> None:
    world.policies = PolicySet(retention={h.LEDGER: RetentionAction.DELETE})
    assert not one(world.verify(), CT.RESIDUAL_VALUE_SCAN).passed


def test_residual_scan_without_probes(world: Any) -> None:
    world.probes = []
    world.reader.calls.clear()
    c = one(world.verify(), CT.RESIDUAL_VALUE_SCAN)
    assert c.passed
    assert (c.observed["probe_count"], c.observed["columns_scanned"]) == (0, 0)
    assert "hmac_match_count" not in world.reader.calls


def test_residual_scan_deduplicates_columns(world: Any) -> None:
    world.scan_columns = world.scan_columns * 2
    c = one(world.verify(), CT.RESIDUAL_VALUE_SCAN)
    assert c.observed["columns_scanned"] == 7


@pytest.mark.parametrize("digest", ["A" * 64, "a" * 63, "g" * 64, ""])
def test_malformed_probe_digest(world: Any, h: ModuleType, digest: str) -> None:
    from lae.contracts.enums import Normalization

    world.probes = [ResidualProbe(ColumnRef(h.CUSTOMER, "email"), Normalization.EXACT, digest)]
    with pytest.raises(VerificationError) as ei:
        world.verify()
    assert ei.value.code == "INVALID_PROBE"


# ------------------------------------------------------------------ outcome / coverage


def test_unresolved_item_gives_partial(world: Any, h: ModuleType) -> None:
    world.items.append(
        h.item(10, PlanAction.UNRESOLVED, h.t("external_crm"), level=LineageLevel.TABLE)
    )
    r = world.verify()
    assert r.status is VerificationStatus.PASSED
    assert r.outcome is VerificationOutcome.PARTIAL
    assert (r.required_artifacts, r.verified_artifacts) == (10, 9)
    assert r.coverage == Fraction(9, 10)
    assert find(r, CT.ROW_ABSENT, 10) == []


@pytest.mark.parametrize("action", [PlanAction.SCHEDULE_EXTERNAL, PlanAction.MANUAL_REVIEW])
def test_schedule_and_manual_review_never_verified(
    world: Any, h: ModuleType, action: PlanAction
) -> None:
    world.items.append(h.item(10, action, h.ORDER, keys=((200,),)))
    world.items.append(h.item(11, action, h.t("x"), level=LineageLevel.TABLE))
    r = world.verify()
    assert r.outcome is VerificationOutcome.PARTIAL
    assert (r.required_artifacts, r.verified_artifacts) == (11, 9)


def test_failure_beats_partial(world: Any, h: ModuleType) -> None:
    world.items.append(h.item(10, PlanAction.UNRESOLVED, h.t("x"), level=LineageLevel.TABLE))
    world.rows(h.CACHE).append({"customer_id": h.SUBJECT, "payload": "{}"})
    r = world.verify()
    assert r.outcome is VerificationOutcome.FAILED
    assert (r.required_artifacts, r.verified_artifacts) == (10, 8)
    assert r.coverage == Fraction(4, 5)


def test_empty_plan_is_verified(world: Any, h: ModuleType) -> None:
    world.items = []
    r = world.verify()
    assert r.outcome is VerificationOutcome.FAILED  # the legal-hold exemption needs a RETAIN item
    world.rows(h.LEDGER).clear()
    r = world.verify()
    assert (r.required_artifacts, r.verified_artifacts) == (0, 0)
    assert r.coverage == 1
    assert r.outcome is VerificationOutcome.VERIFIED


# ------------------------------------------------------------------ cannot run


@pytest.mark.parametrize(
    ("mode", "status"),
    [
        (ExecutionMode.DRY_RUN, ExecutionStatus.ROLLED_BACK),
        (ExecutionMode.LIVE, ExecutionStatus.ROLLED_BACK),
        (ExecutionMode.LIVE, ExecutionStatus.FAILED),
        (ExecutionMode.LIVE, ExecutionStatus.RUNNING),
    ],
)
def test_only_committed_live_is_verifiable(
    world: Any, mode: ExecutionMode, status: ExecutionStatus
) -> None:
    world.execution = replace(world.execution, mode=mode, status=status)
    with pytest.raises(VerificationError) as ei:
        world.verify()
    assert ei.value.code == "NOT_VERIFIABLE"


@pytest.mark.parametrize("key", [b"short", "x" * 40])
def test_bad_hmac_key(world: Any, key: object) -> None:
    world.hmac_key = key
    with pytest.raises(VerificationError) as ei:
        world.verify()
    assert ei.value.code == "INVALID_HMAC_KEY"


@pytest.mark.parametrize(
    ("method", "check"),
    [
        ("count_by_columns", "RETENTION_HONORED"),
        ("select_by_columns", "REFERENCE_NULLIFIED"),
        ("symmetric_difference_count", "DERIVED_CONSISTENT"),
        ("hmac_match_count", "RESIDUAL_VALUE_SCAN"),
    ],
)
def test_reader_error_is_verification_error_without_pii(
    world: Any, h: ModuleType, method: str, check: str
) -> None:
    world.reader.fail.add(method)
    with pytest.raises(VerificationError) as ei:
        world.verify()
    err = ei.value
    assert err.code == "CHECK_NOT_RUNNABLE"
    assert err.details["cause"] == "TargetQueryError"
    if method != "count_by_columns":
        assert err.details["check_type"] == check
    assert h.SUBJECT_EMAIL not in f"{err} {err.message} {err.details} {err!r}"
    assert err.__cause__ is None


def test_unknown_table_in_plan_cannot_run(world: Any, h: ModuleType) -> None:
    world.items.append(h.item(10, PlanAction.DELETE_ROWS, h.t("nope"), keys=((1,),)))
    with pytest.raises(VerificationError) as ei:
        world.verify()
    assert ei.value.details["table"] == "app.nope"


@pytest.mark.parametrize("bad", [-1, True, "3", None])
def test_invalid_reader_count(world: Any, bad: object) -> None:
    world.reader.count_override = bad if bad is not None else 1.5
    with pytest.raises(VerificationError) as ei:
        world.verify()
    assert ei.value.code == "INVALID_READER_RESULT"


def test_reader_omitting_projected_column(world: Any, h: ModuleType) -> None:
    for r in world.rows(h.INVOICE):
        del r["name"]
    with pytest.raises(VerificationError) as ei:
        world.verify()
    assert ei.value.details["check_type"] == "COLUMNS_ANONYMIZED"


@pytest.mark.parametrize(
    "action",
    [
        PlanAction.DELETE_ROWS,
        PlanAction.EXPECT_CASCADE,
        PlanAction.ANONYMIZE_ROWS,
        PlanAction.NULLIFY_REFERENCE,
        PlanAction.RETAIN_ROWS,
    ],
)
def test_table_level_keyed_action_cannot_run(world: Any, h: ModuleType, action: PlanAction) -> None:
    world.items = [h.item(1, action, h.ORDER, level=LineageLevel.TABLE)]
    with pytest.raises(VerificationError) as ei:
        world.verify()
    assert ei.value.code == "UNCHECKABLE_ITEM"
    assert ei.value.details["step_order"] == 1


def test_refresh_without_artifact_cannot_run(world: Any, h: ModuleType) -> None:
    world.artifacts = [a for a in world.artifacts if a.target != h.LTV]
    with pytest.raises(VerificationError) as ei:
        world.verify()
    assert ei.value.code == "UNCHECKABLE_ITEM"


def test_refresh_without_definition_cannot_run(world: Any, h: ModuleType) -> None:
    world.artifacts = [
        replace(a, definition_sql=None) if a.target == h.LTV else a for a in world.artifacts
    ]
    with pytest.raises(VerificationError):
        world.verify()


def test_ambiguous_artifact_target(world: Any) -> None:
    world.artifacts.append(world.artifacts[0])
    with pytest.raises(VerificationError) as ei:
        world.verify()
    assert ei.value.code == "AMBIGUOUS_ARTIFACT"


def test_verification_error_from_callback_passes_through(world: Any) -> None:
    def broken(_reader: object) -> Any:
        raise VerificationError("inner", code="INNER")

    world.rediscover = broken
    with pytest.raises(VerificationError) as ei:
        world.verify()
    assert ei.value.code == "INNER"


def test_anonymize_item_with_no_keys_skips_reader(world: Any, h: ModuleType) -> None:
    world.items = [h.item(1, PlanAction.ANONYMIZE_ROWS, h.INVOICE, target_columns=("email",))]
    world.rows(h.LEDGER).clear()
    world.reader.calls.clear()
    r = world.verify()
    assert one(r, CT.COLUMNS_ANONYMIZED, 1).passed
    assert "select_by_columns" not in world.reader.calls
