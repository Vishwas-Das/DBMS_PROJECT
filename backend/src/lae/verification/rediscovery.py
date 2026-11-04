"""REDISCOVERY_EMPTY: re-run record discovery on fresh data (architecture.md §12)."""

from __future__ import annotations

from collections.abc import Callable

from lae.contracts.enums import CheckType, PlanAction, RecordRole
from lae.contracts.ports import TargetReader
from lae.contracts.types import CheckResult, ErasurePlan, KeyTuple, RecordKey, TableRef
from lae.errors import SubjectNotFoundError
from lae.lineage.results import RecordDiscoveryResult
from lae.verification._common import guarded

_RETAINING_ACTIONS = frozenset({PlanAction.RETAIN_ROWS, PlanAction.ANONYMIZE_ROWS})
_SUBJECT_ROLES = frozenset({RecordRole.OWNED, RecordRole.SUBJECT_ROOT})

RetainedKey = tuple[TableRef, tuple[str, ...], KeyTuple]


def retained_keys(plan: ErasurePlan) -> set[RetainedKey]:
    """Rows the plan deliberately keeps (RETAIN_ROWS / ANONYMIZE_ROWS items)."""
    return {
        (it.table, it.key_columns, key)
        for it in plan.items
        if it.action in _RETAINING_ACTIONS
        for key in it.record_keys
    }


def _is_retained(key: RecordKey, retained: set[RetainedKey]) -> bool:
    return (key.table, key.columns, key.values) in retained


def check_rediscovery(
    reader: TargetReader,
    plan: ErasurePlan,
    rediscover_records: Callable[[TargetReader], RecordDiscoveryResult],
) -> CheckResult:
    """No OWNED / SUBJECT_ROOT record may remain, except rows of RETAIN_ROWS / ANONYMIZE_ROWS
    items. A ``SubjectNotFoundError`` from discovery (the root row is gone) means nothing was
    rediscovered. Limitation: rediscovery walks the same lineage graph from the subject root, so
    it cannot find un-modelled copies; RESIDUAL_VALUE_SCAN covers those."""
    ct = CheckType.REDISCOVERY_EMPTY

    def run() -> RecordDiscoveryResult | None:
        try:
            return rediscover_records(reader)
        except SubjectNotFoundError:
            return None

    result = guarded(ct, None, run)
    retained = retained_keys(plan)
    remaining: dict[TableRef, int] = {}
    kept = 0
    found_root = False
    if result is not None:
        for key, attrs in result.record_graph.nodes():
            if attrs.role is RecordRole.SUBJECT_ROOT:
                found_root = True
            if attrs.role not in _SUBJECT_ROLES:
                continue
            if _is_retained(key, retained):
                kept += 1
            else:
                remaining[key.table] = remaining.get(key.table, 0) + 1
    total = sum(remaining.values())
    return CheckResult(
        ct,
        None,
        None,
        None,
        total == 0,
        observed={
            "remaining_records": total,
            "retained_records": kept,
            "subject_root_found": found_root,
            "remaining_by_table": [
                {"table": t.qualified(), "count": n} for t, n in sorted(remaining.items())
            ],
        },
        expected={"remaining_records": 0},
    )
