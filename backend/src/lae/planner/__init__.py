"""lae.planner: policy-aware erasure planning (module_contracts.md §7.1)."""

from __future__ import annotations

from lae.planner.hashing import compute_plan_hash, plan_hash_payload, plan_with_hash
from lae.planner.planner import build_plan, detect_conflicts, pseudonym_columns
from lae.planner.state import (
    ACKNOWLEDGEABLE_CODES,
    TRANSITIONS,
    acknowledge_conflict,
    check_approval,
    check_transition,
    initial_status,
)
from lae.planner.text import CASCADE_CHECK_TOKEN, DEFINITION_TOKEN, definition_token

__all__ = [
    "ACKNOWLEDGEABLE_CODES",
    "CASCADE_CHECK_TOKEN",
    "DEFINITION_TOKEN",
    "TRANSITIONS",
    "acknowledge_conflict",
    "build_plan",
    "check_approval",
    "check_transition",
    "compute_plan_hash",
    "definition_token",
    "detect_conflicts",
    "initial_status",
    "plan_hash_payload",
    "plan_with_hash",
    "pseudonym_columns",
]
