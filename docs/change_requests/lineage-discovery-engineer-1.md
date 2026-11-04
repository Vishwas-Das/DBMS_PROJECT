# Change request lineage-discovery-engineer-1: `lae.discovery` contract clarifications (stages 3–5)

Status: OPEN, awaiting Lead review. No contract files were edited. Everything below is
implemented in `backend/src/lae/discovery/` and can be reversed there if the Lead decides
otherwise. All additions are backward compatible (keyword-only or new names).

## 1. Additive API beyond module_contracts.md §5
| Name | Why |
|---|---|
| `extract_sql_lineage(..., *, extra_keys=None)` | Candidate keys per source table beyond its PK (see §2). |
| `extract_artifacts_lineage(artifacts, catalog, *, extra_keys=None)` | Runs §7 lineage for all artifacts, propagating KEY_PRESERVING target columns as keys of the artifact for downstream artifacts (customer_ltv → customer_segment). Iterates to a fixpoint. |
| `build_catalog(tables, fks, schemas)`, `catalog_fingerprint(...)` | Pure builders used by `read_catalog`; the service / RediscoverFn can recompute the fingerprint without a second catalog read. |
| `schema_drift(old, new) -> SchemaDrift(added_tables, removed_tables, changed_tables, fingerprint_changed)` | FK changes are not visible in `CatalogSnapshot.tables`, so `fingerprint_changed` is the authoritative flag; `has_drift` combines both. |
| `foreign_key_edges(rows, catalog)`, `fk_edge_id(...)` | Pure conversion (reuses rows already read). |
| `artifacts_from_catalog`, `merge_registered`, `external_dependencies`, `inspectable_artifacts`, `auto_artifact_id` | Artifact registry helpers. An artifact whose target is *not* in the catalog is treated like EXTERNAL. |
| `infer_relationships(..., *, exclude=())` | Known edges (declared FKs) to skip. |
| `capture_residual_probes(..., keyring, *, batch_size=1000)` | The §5 table calls the last parameter `hmac`; it is an `HmacKeyring`. |
| `record_set_fingerprint(graph, root, key, reader, limits)`, `probe_normalizations(column, value)` | Helpers for the executor's rediscover and for verification. |

## 2. Candidate keys for KEY_PRESERVING (architecture.md §7 step 5)
§7 says "the subject-reachable key of that source (its PK, or an FK column referencing the
subject root chain)". `extract_sql_lineage(artifact, catalog)` has no FK or subject-root input,
so by default only the **PK** of a source counts. Treating every FK-looking column as a key would
be unsound: `SELECT region_id, sum(x) FROM purchase_order GROUP BY region_id` would make a shared
regional aggregate row look owned by the subject. Consequence: `SELECT customer_id, sum(..) FROM
purchase_order GROUP BY customer_id` is **AGGREGATE** (refresh), which is safe but not exact.
**Proposal:** the service passes `extra_keys` = FK child columns whose parent is the subject root
or reachable from it (it has both the FK edges and the subject roots), plus nothing else.
`extract_artifacts_lineage` already adds upstream artifact key columns automatically.

## 3. Materialized views have no primary key
PostgreSQL cannot declare a PK on a matview (`customer_ltv` only has a unique index). With
`primary_key=()`, discovery addresses its rows by predicate (`RecordKey(is_predicate=True)`,
columns = the KP target columns) and propagates the key to downstream artifacts (§2).
**Question for the Database Engineer / Lead:** should `PgCatalogReader.list_tables` report the
columns of a unique, non-partial index of a matview as `primary_key`? Discovery works either way;
the planner/verification must accept predicate keys on matviews (they are refreshed anyway).

## 4. `affected_artifacts` semantics
Value per artifact table = the most severe traceability among the traversed edges into it:
**OPAQUE > AGGREGATE > KEY_PRESERVING**. KEY_PRESERVING artifacts appear only if at least one row
was found. Example: `customer_ltv` is AGGREGATE (reached through the AGGREGATE edges from
`purchase_order` / `payment`) **and** its row for customer 42 is in the record graph as an OWNED
predicate record. The planner should therefore refresh the matview; deleting by key is not
required for a matview. A TABLE edge with KEY_PRESERVING but no key columns is treated as OPAQUE
(warning).

## 5. SQL-lineage edge conventions (not specified in the contract)
- TABLE-edge transform: KEY_PRESERVING → `IDENTITY`, AGGREGATE → `AGGREGATE`, OPAQUE → `EXPRESSION`.
  Only KEY_PRESERVING TABLE edges carry columns (the key mapping).
- Control lineage (FILTER / JOIN_KEY) has no output column, but `lineage_edge` requires COLUMN
  edges to have one pair. Discovery emits one single-pair COLUMN edge from the control column to
  **each** output column of the artifact. Columns that appear only in `GROUP BY` are `FILTER`
  (`details.clause`). A column that also flows into an output gets no control edge (§7: "only").
- SQL that cannot be analysed (parse/qualify failure, recursive CTE, LATERAL/SRF, unknown function,
  table outside the catalog) yields OPAQUE TABLE edges only (no invented column edges). When the
  SQL does not even parse, the source tables come from a conservative text scan of catalog names.
- Per-source rule: KEY_PRESERVING if a candidate key survives as IDENTITY (grouped where it crosses
  an aggregating scope); else AGGREGATE if every value path is aggregated (or the source is
  control-only); else OPAQUE (values flow without a key). Artifact = OPAQUE if any edge is OPAQUE,
  else KEY_PRESERVING if any is, else AGGREGATE. No source tables at all ⇒ OPAQUE.
- `DISTINCT` / `DISTINCT ON` count as grouping; `ROLLUP`/`CUBE`/`GROUPING SETS` never preserve keys.
- Join equivalence (`c.id = o.customer_id`) is **not** used to transfer keys between tables (v1).
- Warnings never contain SQL text or literals: table/column/function names and exception class
  names only (a definition can embed constants that are personal data).

## 6. Record discovery details
- `policies` is accepted and ignored: roles do not depend on retention (the planner applies it).
- `root.key_columns` must equal the root table's PK, and the key length must match
  (`ValidationError`). Missing root row ⇒ `SubjectNotFoundError(details={"table"})`, no key values.
- SHARED fixpoint uses references = CONFIRMED traversable TABLE edges into the row's table whose
  source is subject-reachable and that are DECLARED_FK or map the source's full PK. Confirmed
  INFERRED e-mail edges therefore never make a row SHARED. "Outside the subject's set" means not
  SUBJECT_ROOT and not OWNED (REFERENCING_OTHER_SUBJECT rows are outside).
- Limits: `max_depth` counts hops from the root; a *new* record beyond it raises. `max_records`
  counts all records incl. root and REFERENCING_OTHER_SUBJECT. Details: `{limit, value, table}`.
- Key normalisation for `RecordKey`: `UUID → str`, integral `Decimal → int`, other non-int/str
  values → `str(v)`. **Database Engineer:** `select_by_columns` must accept these str values for
  non-text key columns (e.g. bind with an explicit cast to the column type).

## 7. Residual probes
Root identifier columns + confirmed columns that are `DIRECT_IDENTIFIER` **or**
`use_for_residual_scan=True`, read from the root and OWNED rows only (SHARED rows belong partly to
someone else). Normalisations: EXACT always, LOWER_TRIM for e-mail-like columns/values,
DIGITS_ONLY for phone-like columns; values that normalise to empty are skipped. Batched through
`select_by_columns` (not `fetch_columns`).

## 8. Ids and numbers
- FK edge id = uuid5(NS_FK, origin, **child table**, constraint, level, pair index): constraint
  names are unique per table in PostgreSQL, not per schema.
- SQL edge id = uuid5(NS_SQL, artifact_id, origin, level, transform, source, cols, target, cols).
- Auto-registered artifacts: id = uuid5(NS_ARTIFACT, "auto", qualified name), name = qualified name.
- Inferred `confidence` = containment × name weight, rounded **down** to 3 decimals and capped at
  0.999 (fits `numeric(4,3)` and the `INFERRED ⇒ confidence < 1` check).

## 9. Logging (service-level configuration)
SQLGlot 30.29 logs `'<sql>' contains unsupported syntax. Falling back to parsing as a 'Command'.`
at WARNING on logger `sqlglot`, echoing the SQL text. Discovery must not mutate global logging
state, so **proposal:** the service/app logging config sets logger `sqlglot` to ERROR.

---
## Lead decision (2026-11-04): ACCEPTED
1. The additive API is accepted as part of the contract.
2. **Yes:** the service passes FK child columns that reference the subject-root chain as `extra_keys`, so `GROUP BY customer_id` artifacts become KEY_PRESERVING.
3. Matview unique indexes are not reported as a PK (catalog fidelity). Discovery's handling is fine.
4, 5, 7, 8: accepted as implemented.
6. Accepted. The DB Engineer's `select_by_columns` casts bound values to the column type (`%s::<format_type>` via a validated type name) so str-normalised keys work.
9. Accepted: `lae.services` logging config sets logger `sqlglot` to ERROR.
