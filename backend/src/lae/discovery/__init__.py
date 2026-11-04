"""lae.discovery: catalog, FK, SQL-lineage, inference, PII and record discovery (§5).

Only `lae.contracts`, `lae.errors`, `lae.lineage`, `sqlglot` and the hashing helpers of
`lae.audit` are imported. Database access goes through the ports in `lae.contracts.ports`.
"""

from __future__ import annotations

from lae.discovery.artifacts import (
    artifacts_from_catalog,
    auto_artifact_id,
    external_dependencies,
    inspectable_artifacts,
    merge_registered,
)
from lae.discovery.catalog import (
    SchemaDrift,
    build_catalog,
    catalog_fingerprint,
    read_catalog,
    schema_drift,
)
from lae.discovery.foreign_keys import discover_foreign_keys, fk_edge_id, foreign_key_edges
from lae.discovery.inference import infer_relationships
from lae.discovery.pii import suggest_pii
from lae.discovery.records import (
    capture_residual_probes,
    discover_records,
    probe_normalizations,
    record_set_fingerprint,
)
from lae.discovery.sql_lineage import (
    SQLGLOT_VERSION,
    extract_artifacts_lineage,
    extract_sql_lineage,
    sql_edge_id,
)

__all__ = [
    "SQLGLOT_VERSION",
    "SchemaDrift",
    "artifacts_from_catalog",
    "auto_artifact_id",
    "build_catalog",
    "capture_residual_probes",
    "catalog_fingerprint",
    "discover_foreign_keys",
    "discover_records",
    "external_dependencies",
    "extract_artifacts_lineage",
    "extract_sql_lineage",
    "fk_edge_id",
    "foreign_key_edges",
    "infer_relationships",
    "inspectable_artifacts",
    "merge_registered",
    "probe_normalizations",
    "read_catalog",
    "record_set_fingerprint",
    "schema_drift",
    "sql_edge_id",
    "suggest_pii",
]
