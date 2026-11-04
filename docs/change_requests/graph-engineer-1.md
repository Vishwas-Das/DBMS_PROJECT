# Change request graph-engineer-1: `lae.lineage` contract clarifications (M1 / VS-1)

Status: OPEN, awaiting Lead review. No contract files were edited. The implementation follows the
choices below, and each one can be reversed in `backend/src/lae/lineage/` if the Lead decides otherwise.

## 1. `build_lineage_graph(..., pii = {})`: mutable default
module_contracts.md §6 shows `pii: Mapping[ColumnRef, PiiColumn] = {}`. The implementation uses
`pii: Mapping[ColumnRef, PiiColumn] | None = None` (ruff B006). Callers see the same behaviour.
**Proposal:** update §6 to `| None = None`.

## 2. `DeletionOrder` field types
§6 comment says `order: list[RecordKey], break_edges: list[(parent, child, edge_id)]`. The task
brief and the frozen-dataclass convention say tuples. The implementation uses
`order: tuple[RecordKey, ...]` and `break_edges: tuple[tuple[RecordKey, RecordKey, UUID], ...]`.
**Proposal:** update §6 to tuples.

## 3. Extra optional `statuses` parameters (backward compatible)
- `reachable_from(table, statuses=CONFIRMED_ONLY)` and `table_cycles(statuses=CONFIRMED_ONLY)`.
- `to_node_link(level, statuses=<all>)`. By default it includes **all** statuses, so the UI can show
  SUGGESTED edges for confirmation. `GraphEdgeOut.status` lets the client filter.
- `artifact_order` always uses CONFIRMED edges.
**Proposal:** document these defaults in §6.

## 4. `to_node_link` shape vs `GraphOut`
It returns `{"nodes": [...], "edges": [...]}` only. `snapshot_id` and `level` belong to `GraphOut` and
are added by the service layer. Node/edge dicts match GraphNodeOut / GraphEdgeOut field for field.
Node-level choices: `table_kind` is set for table nodes and `None` for column nodes. A column node's
`is_artifact` is its table's value.
**Ambiguity:** for a COLUMN-level `RelationshipEdge` with more than one column pair, one
GraphEdgeOut is emitted **per pair**, and they share the same `id`. If discovery always emits
single-pair COLUMN edges (as `fk_pair` in the tests does), ids are unique. **Proposal:** either state
in §5/§6 that COLUMN edges are single-pair, or give GraphEdgeOut a composite `id` (`"<uuid>:<i>"`).

## 5. `is_artifact` definition
A table node gets `is_artifact=True` when its catalog kind is VIEW / MATERIALIZED_VIEW, or when it is
the target of any edge with a non-null `artifact_id`. Registered DERIVED_TABLE / EMBEDDING_TABLE
artifacts that have no edge yet are therefore not flagged. **Proposal:** `build_lineage_graph`
could accept an optional `artifacts: Iterable[TableRef]` argument.

## 6. RecordGraph semantics that were not specified
- **Role on re-visit:** the most protective role wins (SUBJECT_ROOT > REFERENCING_OTHER_SUBJECT >
  SHARED > OWNED). `add_dependency(..., role=SUBJECT_ROOT)` is rejected, and `add_root` must be used.
  `add_root` on an existing node upgrades it to SUBJECT_ROOT at depth 0.
- **Unknown parent:** `add_dependency` raises `GraphIntegrityError(code="UNKNOWN_PARENT")`.
- **Several lineage edges between the same two rows** (e.g. billing and shipping FKs) are kept as a set
  on one nx edge. `edges()` yields one triple per edge id. Breaking that graph edge requires
  **every** edge id on it to be nullable, and all of them are returned in `break_edges`.
- **Single-row self-references** (row references itself) are ignored for ordering, because one
  DELETE removes the row and its own reference. They never appear in `break_edges`. Please confirm
  this is acceptable for RESTRICT FKs.
- **SCC breaking strategy:** remove all nullable intra-SCC edges. If the SCC is still cyclic, raise
  `UNBREAKABLE_CYCLE` (details: table names + record count only, no key values). Otherwise restore
  removed edges in deterministic order when that keeps the graph acyclic. The result is a minimal
  (not necessarily minimum) set of NULLIFY_REFERENCE breaks.
- **`to_node_link(key_hmac)` record shape** (not in database_schema.md §6): nodes
  `{id, table, key_columns, is_predicate, role, depth, via_edge_ids}`, edges `{source, target, edge_id}`.
  Nodes are sorted by `(table, id)` so the order leaks nothing about raw values. **Proposal:** add
  a `RecordGraphOut` schema.

## 7. Unknown tables in queries
`downstream`, `upstream`, `reachable_from` and `artifact_order` raise `UnknownTableError` (via
`CatalogSnapshot.table`) for a table that is not in the catalog. Only construction raises
`GraphIntegrityError`.

## 8. `RecordDiscoveryResult`
Kept as `@dataclass(frozen=True, slots=True)`. `__post_init__` freezes `affected_artifacts` into a
`MappingProxyType` sorted by TableRef. Equality and hash follow the dataclass default. Hashing an
instance raises `TypeError` because RecordGraph is unhashable. **Proposal:** consider `eq=False`
as used for `CatalogSnapshot`.

---
## Lead decision (2026-11-04): ACCEPTED
1–3, 6, 7: accepted as implemented. They are now part of the contract (module_contracts.md §6 note).
4: **COLUMN-level edges are single-pair.** Discovery emits one COLUMN edge per column pair
   (already stated in architecture.md §6). Edge ids are therefore unique, and there's no composite id.
5: deferred to the derived-artifacts milestone (`artifacts` argument added then).
6 (self-reference): accepted. PostgreSQL deletes a row that references itself in one DELETE, even under RESTRICT.
8: accepted. Graph Engineer to switch `RecordDiscoveryResult` to `eq=False` in the next lineage task.
