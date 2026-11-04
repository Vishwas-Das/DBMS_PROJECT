"""lae.executor: safe, hash-bound plan execution (module_contracts.md §7.2)."""

from __future__ import annotations

from lae.executor.executor import DEFINITION_TOKEN, execute_plan

__all__ = ["DEFINITION_TOKEN", "execute_plan"]
