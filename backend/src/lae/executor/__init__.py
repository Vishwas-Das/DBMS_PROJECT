"""lae.executor: safe, hash-bound plan execution (module_contracts.md §7.2)."""

from __future__ import annotations

from lae.executor.executor import CASCADE_CHECK_RE, DEFINITION_TOKEN, execute_plan

__all__ = ["CASCADE_CHECK_RE", "DEFINITION_TOKEN", "execute_plan"]
