"""Per-plan-item checks (architecture.md §12, CR lead-1 §3 / §7).

Every check is bound to the item's ``step_order``. ``observed`` / ``expected`` hold counts only
(never key or column values). A check that fails returns ``passed=False``; a check that cannot
run raises ``VerificationError``.

| PlanAction                                        | Check(s)                                 |
|---------------------------------------------------|------------------------------------------|
| DELETE_ROWS, DELETE_DERIVED_BY_KEY, INVALIDATE_CACHE | ROW_ABSENT                            |
| EXPECT_CASCADE                                    | CASCADE_COMPLETE                         |
| ANONYMIZE_ROWS                                    | COLUMNS_ANONYMIZED (one per target column)|
| NULLIFY_REFERENCE                                 | REFERENCE_NULLIFIED (one per target column)|
| RETAIN_ROWS                                       | RETENTION_HONORED                        |
| REFRESH_MATVIEW, RECOMPUTE_TABLE                  | DERIVED_CONSISTENT (+ ROW_ABSENT if keyed)|
| SCHEDULE_EXTERNAL, MANUAL_REVIEW, UNRESOLVED      | none (never verifiable in the database)  |
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from lae.contracts.enums import ArtifactKind, CheckType, LineageLevel, PlanAction
from lae.contracts.ports import TargetReader
from lae.contracts.types import (
    ArtifactDefinition,
    CheckResult,
    ErasurePlan,
    KeyTuple,
    PlanItem,
    TableRef,
)
from lae.errors import VerificationError
from lae.verification._common import (
    as_count,
    deleted_keys,
    guarded,
    require_record_level,
    sorted_unique_keys,
)

PSEUDONYM_PATTERN = re.compile(r"anon_[0-9a-f]{16}")
"""A pseudonymized value (matched with ``fullmatch``; equivalent to SQL ``^anon_[0-9a-f]{16}$``)."""

_ABSENT_ACTIONS = {
    PlanAction.DELETE_ROWS: CheckType.ROW_ABSENT,
    PlanAction.DELETE_DERIVED_BY_KEY: CheckType.ROW_ABSENT,
    PlanAction.INVALIDATE_CACHE: CheckType.ROW_ABSENT,
    PlanAction.EXPECT_CASCADE: CheckType.CASCADE_COMPLETE,
}
_DERIVED_ACTIONS = frozenset({PlanAction.REFRESH_MATVIEW, PlanAction.RECOMPUTE_TABLE})


def is_anonymized(value: object) -> bool:
    """NULL, or a string that is exactly ``anon_`` + 16 lowercase hex characters."""
    return value is None or (
        isinstance(value, str) and PSEUDONYM_PATTERN.fullmatch(value) is not None
    )


def _count_keys(
    reader: TargetReader,
    check_type: CheckType,
    table: TableRef,
    columns: Sequence[str],
    keys: Sequence[KeyTuple],
) -> int:
    if not keys:
        return 0
    return as_count(
        guarded(check_type, table, lambda: reader.count_by_columns(table, columns, keys)),
        check_type,
        table,
    )


def _select_rows(
    reader: TargetReader, check_type: CheckType, item: PlanItem
) -> list[dict[str, Any]]:
    if not item.record_keys:
        return []
    select = tuple(dict.fromkeys((*item.key_columns, *item.target_columns)))

    def run() -> list[dict[str, Any]]:
        rows = reader.select_by_columns(item.table, item.key_columns, item.record_keys, select)
        for row in rows:
            for col in select:
                if col not in row:
                    raise KeyError("projected column missing from reader result")
        return list(rows)

    return guarded(check_type, item.table, run)


# ------------------------------------------------------------------ individual checks


def check_absent(reader: TargetReader, item: PlanItem) -> CheckResult:
    """ROW_ABSENT / CASCADE_COMPLETE: none of the item's keys still match a row."""
    check_type = _ABSENT_ACTIONS[item.action]
    require_record_level(item, check_type)
    remaining = _count_keys(reader, check_type, item.table, item.key_columns, item.record_keys)
    return CheckResult(
        check_type,
        item.table,
        None,
        item.step_order,
        remaining == 0,
        observed={"remaining_rows": remaining},
        expected={"remaining_rows": 0, "planned_keys": len(item.record_keys)},
    )


def check_anonymized(reader: TargetReader, item: PlanItem) -> list[CheckResult]:
    """COLUMNS_ANONYMIZED: retained rows still exist and every target column is NULL or a
    pseudonym. One check per target column (a single ``column=None`` check if there are none)."""
    ct = CheckType.COLUMNS_ANONYMIZED
    require_record_level(item, ct)
    rows = _select_rows(reader, ct, item)
    present = len(rows)
    columns: Sequence[str | None] = item.target_columns or (None,)
    out: list[CheckResult] = []
    for col in columns:
        violations = 0 if col is None else sum(1 for r in rows if not is_anonymized(r[col]))
        out.append(
            CheckResult(
                ct,
                item.table,
                col,
                item.step_order,
                violations == 0 and present == item.expected_row_count,
                observed={"rows_present": present, "violations": violations},
                expected={"rows_present": item.expected_row_count, "violations": 0},
            )
        )
    return out


def check_nullified(reader: TargetReader, item: PlanItem, plan: ErasurePlan) -> list[CheckResult]:
    """REFERENCE_NULLIFIED: every target column of the item's rows is NULL.

    A row of the item that no longer exists is acceptable only if a DELETE-category item of the
    same plan removes it (references broken before deleting a cycle); otherwise it is counted as
    ``rows_missing`` and the check fails (a row the subject does not own disappeared).
    """
    ct = CheckType.REFERENCE_NULLIFIED
    require_record_level(item, ct)
    rows = _select_rows(reader, ct, item)
    present_keys = {tuple(r[c] for c in item.key_columns) for r in rows}
    missing = [k for k in sorted_unique_keys(item.record_keys) if k not in present_keys]
    removed = deleted_keys(plan.items, item.table, item.key_columns)
    deleted_by_plan = sum(1 for k in missing if k in removed)
    unexplained = len(missing) - deleted_by_plan
    columns: Sequence[str | None] = item.target_columns or (None,)
    out: list[CheckResult] = []
    for col in columns:
        violations = 0 if col is None else sum(1 for r in rows if r[col] is not None)
        out.append(
            CheckResult(
                ct,
                item.table,
                col,
                item.step_order,
                violations == 0 and unexplained == 0,
                observed={
                    "rows_present": len(rows),
                    "rows_deleted_by_plan": deleted_by_plan,
                    "rows_missing": unexplained,
                    "violations": violations,
                },
                expected={"rows_missing": 0, "violations": 0},
            )
        )
    return out


def check_retained(reader: TargetReader, item: PlanItem) -> CheckResult:
    """RETENTION_HONORED: the legally retained rows still exist with the expected count."""
    ct = CheckType.RETENTION_HONORED
    require_record_level(item, ct)
    present = _count_keys(reader, ct, item.table, item.key_columns, item.record_keys)
    return CheckResult(
        ct,
        item.table,
        None,
        item.step_order,
        present == item.expected_row_count,
        observed={"rows_present": present},
        expected={"rows_present": item.expected_row_count},
    )


def subject_source_keys(
    plan: ErasurePlan, artifact: ArtifactDefinition
) -> list[tuple[tuple[str, ...], tuple[KeyTuple, ...]]]:
    """For each key mapping of ``artifact``: (target columns, subject keys deleted upstream).

    The subject's keys are taken from the plan's DELETE-category items on the mapping's source
    table, projected onto the mapping's source columns. Mappings without such keys are omitted.
    """
    out: list[tuple[tuple[str, ...], tuple[KeyTuple, ...]]] = []
    for m in artifact.key_mappings:
        keys = sorted_unique_keys(deleted_keys(plan.items, m.source, m.source_columns))
        if keys:
            out.append((tuple(m.target_columns), keys))
    return out


def _absent_by_mappings(
    reader: TargetReader,
    check_type: CheckType,
    target: TableRef,
    mapped: Sequence[tuple[tuple[str, ...], tuple[KeyTuple, ...]]],
) -> tuple[int, int]:
    remaining = 0
    source_keys = 0
    for cols, keys in mapped:
        remaining += _count_keys(reader, check_type, target, cols, keys)
        source_keys += len(keys)
    return remaining, source_keys


def check_derived(
    reader: TargetReader,
    item: PlanItem,
    plan: ErasurePlan,
    artifacts: Mapping[TableRef, ArtifactDefinition],
) -> list[CheckResult]:
    """DERIVED_CONSISTENT (stored vs. freshly evaluated definition is empty) plus, for
    key-addressable artifacts, ROW_ABSENT of the subject's derived rows.

    The subject's derived rows are the item's own keys if it is RECORD-level with keys,
    otherwise they are derived through the artifact's key mappings (see subject_source_keys).
    """
    ct = CheckType.DERIVED_CONSISTENT
    artifact = artifacts.get(item.table)
    if artifact is None or not artifact.definition_sql:
        raise VerificationError(
            "DERIVED_CONSISTENT needs a registered artifact with a definition",
            code="UNCHECKABLE_ITEM",
            details={"table": item.table.qualified(), "step_order": item.step_order},
        )
    definition_sql = artifact.definition_sql
    diff = as_count(
        guarded(
            ct, item.table, lambda: reader.symmetric_difference_count(item.table, definition_sql)
        ),
        ct,
        item.table,
    )
    out = [
        CheckResult(
            ct,
            item.table,
            None,
            item.step_order,
            diff == 0,
            observed={"difference_rows": diff},
            expected={"difference_rows": 0},
        )
    ]
    if item.lineage_level is LineageLevel.RECORD and item.record_keys:
        mapped = [(tuple(item.key_columns), sorted_unique_keys(item.record_keys))]
    else:
        mapped = subject_source_keys(plan, artifact)
    if mapped:
        remaining, n_keys = _absent_by_mappings(reader, CheckType.ROW_ABSENT, item.table, mapped)
        out.append(
            CheckResult(
                CheckType.ROW_ABSENT,
                item.table,
                None,
                item.step_order,
                remaining == 0,
                observed={"remaining_rows": remaining},
                expected={"remaining_rows": 0, "planned_keys": n_keys},
            )
        )
    return out


def check_embeddings(
    reader: TargetReader, plan: ErasurePlan, artifacts: Sequence[ArtifactDefinition]
) -> list[CheckResult]:
    """EMBEDDING_ABSENT: for every EMBEDDING_TABLE artifact, no row is keyed by a source key the
    plan deleted. Runs whether or not the planner produced an item for the embedding table; the
    check is bound to the first item on that table, if any."""
    ct = CheckType.EMBEDDING_ABSENT
    out: list[CheckResult] = []
    for art in sorted(artifacts, key=lambda a: (a.target, str(a.artifact_id))):
        if art.kind is not ArtifactKind.EMBEDDING_TABLE:
            continue
        mapped = subject_source_keys(plan, art)
        if not mapped:
            continue
        remaining, n_keys = _absent_by_mappings(reader, ct, art.target, mapped)
        step = min((it.step_order for it in plan.items if it.table == art.target), default=None)
        out.append(
            CheckResult(
                ct,
                art.target,
                None,
                step,
                remaining == 0,
                observed={"remaining_rows": remaining},
                expected={"remaining_rows": 0, "source_keys": n_keys},
            )
        )
    return out


def checks_for_item(
    reader: TargetReader,
    item: PlanItem,
    plan: ErasurePlan,
    artifacts: Mapping[TableRef, ArtifactDefinition],
) -> list[CheckResult]:
    """All checks bound to one plan item (empty for unverifiable categories)."""
    if item.action in _ABSENT_ACTIONS:
        return [check_absent(reader, item)]
    if item.action is PlanAction.ANONYMIZE_ROWS:
        return check_anonymized(reader, item)
    if item.action is PlanAction.NULLIFY_REFERENCE:
        return check_nullified(reader, item, plan)
    if item.action is PlanAction.RETAIN_ROWS:
        return [check_retained(reader, item)]
    if item.action in _DERIVED_ACTIONS:
        return check_derived(reader, item, plan, artifacts)
    return []  # SCHEDULE_EXTERNAL, MANUAL_REVIEW, UNRESOLVED
