"""Record-discovery result (module_contracts.md §6).

Lives in lae.lineage because it holds a RecordGraph; produced by discovery and consumed by the
planner and verification.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from lae.contracts.enums import Traceability
from lae.contracts.types import TableRef
from lae.lineage.record_graph import RecordGraph

__all__ = ["RecordDiscoveryResult"]


@dataclass(frozen=True, slots=True, eq=False)
class RecordDiscoveryResult:
    """Identity equality (CR graph-engineer-1 item 8): it holds a mutable RecordGraph."""

    record_graph: RecordGraph
    affected_artifacts: Mapping[TableRef, Traceability]
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Freeze and sort the mapping so iteration order is deterministic.
        frozen = MappingProxyType(dict(sorted(self.affected_artifacts.items())))
        object.__setattr__(self, "affected_artifacts", frozen)
        object.__setattr__(self, "warnings", tuple(self.warnings))
