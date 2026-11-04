"""lae.lineage: table/column lineage graph and record-level graph (module_contracts.md §6)."""

from __future__ import annotations

from lae.lineage.graph import (
    CONFIRMED_ONLY,
    LineageGraph,
    build_lineage_graph,
    column_node_id,
    table_node_id,
)
from lae.lineage.record_graph import DeletionOrder, RecordAttrs, RecordGraph
from lae.lineage.results import RecordDiscoveryResult

__all__ = [
    "CONFIRMED_ONLY",
    "DeletionOrder",
    "LineageGraph",
    "RecordAttrs",
    "RecordDiscoveryResult",
    "RecordGraph",
    "build_lineage_graph",
    "column_node_id",
    "table_node_id",
]
