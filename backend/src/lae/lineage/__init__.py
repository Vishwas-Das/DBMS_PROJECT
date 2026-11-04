"""lae.lineage: table/column lineage graph and record-level graph (module_contracts.md §6).

`lae.lineage.api` provides the mission-brief facade functions (build_graph, add_node,
discover_dependents, get_descendants, …) that delegate to the classes exported here.
"""

from __future__ import annotations

from lae.lineage.api import (
    add_edge,
    add_node,
    build_graph,
    detect_cycles,
    discover_dependents,
    get_descendants,
    get_execution_order,
    serialize_graph,
    serialize_record_graph,
)
from lae.lineage.graph import (
    ALL_STATUSES,
    CONFIRMED_ONLY,
    LineageGraph,
    TraversalOrder,
    build_lineage_graph,
    column_node_id,
    edge_evidence,
    table_node_id,
)
from lae.lineage.record_graph import DeletionOrder, RecordAttrs, RecordGraph
from lae.lineage.results import RecordDiscoveryResult

__all__ = [
    "ALL_STATUSES",
    "CONFIRMED_ONLY",
    "DeletionOrder",
    "LineageGraph",
    "RecordAttrs",
    "RecordDiscoveryResult",
    "RecordGraph",
    "TraversalOrder",
    "add_edge",
    "add_node",
    "build_graph",
    "build_lineage_graph",
    "column_node_id",
    "detect_cycles",
    "discover_dependents",
    "edge_evidence",
    "get_descendants",
    "get_execution_order",
    "serialize_graph",
    "serialize_record_graph",
    "table_node_id",
]
