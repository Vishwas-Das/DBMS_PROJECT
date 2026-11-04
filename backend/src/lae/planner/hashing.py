"""Plan hash (architecture.md §10.6): SHA-256 of canonical JSON of everything that affects
execution. Approval must echo it, so a human approves exactly what will run.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from lae.audit.canonical import canonical_hash
from lae.contracts.types import ErasurePlan, PlanConflict, PlanItem

__all__ = ["compute_plan_hash", "plan_hash_payload", "plan_with_hash"]


def _item_payload(item: PlanItem) -> dict[str, Any]:
    return {
        "step_order": item.step_order,
        "action": item.action.value,
        "table": item.table.qualified(),
        "lineage_level": item.lineage_level.value,
        "key_columns": list(item.key_columns),
        "record_keys": [list(k) for k in item.record_keys],
        "target_columns": list(item.target_columns),
        "expected_row_count": item.expected_row_count,
        "depends_on": sorted(item.depends_on),
        "reason_edge_ids": sorted(str(e) for e in item.reason_edge_ids),
        "reason": item.reason,
    }


def _conflict_payload(c: PlanConflict) -> dict[str, Any]:
    return {
        "code": c.code.value,
        "severity": c.severity.value,
        "table": c.table.qualified(),
        "resolved": c.resolved,
    }


def plan_hash_payload(plan: ErasurePlan) -> dict[str, Any]:
    """The canonical structure that is hashed. `plan_id` and `plan_hash` are not part of it.

    Items are ordered by step_order; conflicts are sorted, so the hash does not depend on the
    tuple order. Record keys keep the planner's (RecordKey.sort_key) order.
    """
    conflicts = sorted(
        (_conflict_payload(c) for c in plan.conflicts),
        key=lambda d: (d["code"], d["severity"], d["table"], d["resolved"]),
    )
    return {
        "request_id": str(plan.request_id),
        "schema_fingerprint": plan.schema_fingerprint,
        "record_set_hash": plan.record_set_hash,
        "items": [_item_payload(i) for i in sorted(plan.items, key=lambda i: i.step_order)],
        "conflicts": conflicts,
    }


def compute_plan_hash(plan: ErasurePlan) -> str:
    """SHA-256 hex of the canonical plan payload (the stored `plan.plan_hash` is ignored)."""
    return canonical_hash(plan_hash_payload(plan))


def plan_with_hash(plan: ErasurePlan) -> ErasurePlan:
    """A copy of `plan` whose `plan_hash` is recomputed."""
    return replace(plan, plan_hash=compute_plan_hash(plan))
