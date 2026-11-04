# CR erasure-engine-engineer-1: planner / executor contract gaps (stages 7-8)

Author: Erasure Engine Engineer. Status: **OPEN, awaiting Lead review.** No contract files were
edited. Every item below is implemented in `backend/src/lae/{planner,executor}/` with an
additive, keyword-only, defaulted parameter or a documented choice, so each one can be reversed
there if the Lead decides otherwise.

## 1. Row counts for predicate keys (tables without a PK)
`RecordKey` has no row count, so the planner cannot know how many rows a predicate key
(`is_predicate=True`) matches. `PlanItem` needs `expected_row_count` for RECORD items, and the
executor asserts it.
- **Implemented:** `build_plan(..., *, predicate_row_counts: Mapping[RecordKey, int] | None = None)`.
  Without it, each predicate counts as one row. For a predicate that matches several rows (the
  demo `marketing_event_log` has two rows for customer 42), execution then fails safely with
  `RowCountMismatch` at lock time (full rollback, no data changed).
- **Proposal:** discovery records the count, either as `RecordDiscoveryResult.predicate_row_counts`
  or as `RecordAttrs.row_count: int | None`. Until then the service can compute it with
  `TargetReader.count_by_columns` inside the discovery transaction and pass it in.

## 2. RECOMPUTE_TABLE needs its definition SQL
`TargetWriter.recompute_table(table, definition_sql)` needs the SQL, but `PlanItem` doesn't carry
it, and the SQL must be bound by the approved hash.
- **Implemented:** the planner writes `definition_sha256=<sha256 of the SQL>` into the item
  `reason`, which is part of the plan hash. `execute_plan(..., recompute_sql: Mapping[TableRef, str])`
  receives the SQL. The executor raises `PlanError(code="MISSING_DEFINITION")` if the SQL is
  missing and `PlanHashMismatch` if its SHA-256 differs from the bound token.
- **Proposal:** add `PlanItem.definition_sha256: str | None` (and a `plan_item.definition_sha256
  char(64)` column) instead of using the reason text.

## 3. Per-column ANONYMIZE strategy is not in the plan
`PlanItem.target_columns` doesn't say which column gets NULL and which gets PSEUDONYM, so a policy
change after approval would go unnoticed.
- **Implemented:** `execute_plan(..., pseudonym_columns: Collection[ColumnRef] = ())`. Every other
  target column gets NULL. The helper `lae.planner.pseudonym_columns(plan, policies, lineage)`
  computes the set: confirmed PSEUDONYMIZE PII columns, excluding FK columns, because a pseudonym
  in an FK column would break the constraint. The strategy also appears in the hashed reason
  (`shipping_name (PSEUDONYMIZE)`), so the reviewer sees it, but the executor does not enforce it.
- **Proposal:** add `PlanItem.assignments: tuple[tuple[str, Literal["NULL","PSEUDONYM"]], ...]`
  (hashed). The `pseudonym_columns` parameter then goes away.

## 4. `execute_plan` keyword extensions and audit/status semantics
Signature as implemented (positional part unchanged from §7.2):
```python
execute_plan(plan, approval, writer, rediscover, audit, mode, *, clock,
             pseudonym_columns=(), recompute_sql=None, plan_hasher=None,
             actor="lae-executor") -> ExecutionResult
```
- `plan_hasher`: the executor may not import `lae.audit.canonical` or `lae.planner`, so it cannot
  re-hash the plan itself. If the service passes `plan_hasher=lae.planner.compute_plan_hash`, a plan
  whose content no longer matches `plan_hash` (DB tampering) gives `PlanHashMismatch`.
  **Proposal:** the service always passes it.
- `approval.plan_id != plan.plan_id` also gives `PlanHashMismatch`.
- **Audit:** for LIVE, the executor appends **one `EXECUTION_COMMITTED`** event through `audit`,
  so it lands in the same transaction as the data. Payload: plan_id, plan_hash, mode, approved_by,
  started_at/finished_at (from `clock`), per-step counts. **The service must not append a second
  one.** DRY_RUN appends nothing, because the event would be rolled back anyway.
- **Returned status:** LIVE gives `COMMITTED` ("all steps applied, the caller commits"). DRY_RUN
  gives `ROLLED_BACK` ("the caller must roll back"). Please confirm, or choose `RUNNING`.
- **Lock phase:** `lock_rows` runs for every RECORD item except SCHEDULE_EXTERNAL / UNRESOLVED
  (their tables are outside the DB). The **locked count is also asserted** against
  `expected_row_count`, which catches drift that the record-set hash cannot see (predicate rows).
- **RETAIN_ROWS** is reported as `APPLIED` with `rows_affected` = the counted rows (nothing is
  modified). SCHEDULE_EXTERNAL / MANUAL_REVIEW / UNRESOLVED are `SKIPPED`.
- **Errors:** `LaeError`s raised by the writer (e.g. `TargetQueryError`, `SerializationConflict`)
  are re-raised with the same type, with `details` extended by `step_order`, `action`, `table` and
  `completed_steps` (counts only). Any other exception becomes
  `ExecutionError(code="STEP_FAILED")`. Its message contains only the exception type, and it is
  raised `from None`, so raw DB messages such as `Key (id)=(42)` never propagate.
  **Request to the Database Engineer:** translate psycopg serialization failures into
  `SerializationConflict` and FK/NOT NULL violations into `TargetQueryError` with PII-free
  messages.
- **API mapping (§9):** new codes `STEP_FAILED` (ExecutionError), `INVALID_PLAN`,
  `MISSING_DEFINITION`, `CONFLICT_NOT_FOUND`, `CONFLICT_ALREADY_RESOLVED`,
  `CONFLICT_NOT_ACKNOWLEDGEABLE` (PlanError) currently fall into 500 `INTERNAL`. Proposal:
  CONFLICT_NOT_FOUND gives 404, the other `CONFLICT_*` codes give 409, INVALID_PLAN /
  MISSING_DEFINITION give 422, STEP_FAILED gives 500.

## 5. `build_plan` signature
§7.1 lists `build_plan(request_id, discovery, lineage, policies, artifacts, *, plan_id)`. As
implemented: `build_plan(request_id, discovery, lineage, policies, artifacts, external=(), *,
plan_id, predicate_row_counts=None)`. EXTERNAL artifacts may come through either list.
`detect_conflicts(discovery, lineage, policies, artifacts, external=())` returns the conflicts of
the same computation. Also exported: `plan_with_hash`, `plan_hash_payload`, `initial_status`,
`check_approval`, `acknowledge_conflict`, `ACKNOWLEDGEABLE_CODES`, `TRANSITIONS`.

## 6. Ordering decisions
- EXPECT_CASCADE checks are a separate phase **after all DELETE_ROWS** (each one depends on the
  DELETE that triggers it), not interleaved with the deletes.
- Derived steps: key-based steps (DELETE_DERIVED_BY_KEY / INVALIDATE_CACHE) come first, then
  RECOMPUTE_TABLE / REFRESH_MATVIEW in **`artifact_order`**. The brief's "RECOMPUTE then REFRESH"
  is used only as a tie-break: `customer_segment` (RECOMPUTE) reads `customer_ltv` (REFRESH) and
  must run after it.
- A row removed by CASCADE that also must be gone before *another* parent is deleted can create
  an impossible order (found by the Hypothesis property test). The planner then deletes such rows
  explicitly and orders again. Only if that also fails does it report UNBREAKABLE_CYCLE.
- Known limitation: if tables reference each other cyclically while the rows do not, the deletes
  are split into ranked batches per table. Constraints between rows of the *same* table are not
  considered in that mode. A violation would fail safely (FK error, full rollback).

## 7. Policy-model conflicts and interpretations (please confirm)
1. **CASCADE_DESTROYS_RETAINED** is raised only when the cascading FK column is **NOT NULL**. A
   nullable reference from a retained row to a deleted row is *detached first*: it goes into the
   ANONYMIZE `target_columns`, or into a NULLIFY_REFERENCE for RETAIN rows, so the cascade never
   reaches the row. **AT-11 as worded does not hold on the demo schema.** With `purchase_order =
   ANONYMIZE` the orders are retained and not deleted, so nothing cascades onto `order_line`.
   The conflict appears for `order_line = ANONYMIZE/RETAIN` under deleted orders (NOT NULL
   CASCADE), and that case is tested. Proposal: reword AT-11 accordingly.
2. **RETAIN (legal hold) rows are modified** when they have a nullable FK to a deleted row
   (NULLIFY_REFERENCE, for RESTRICT/NO ACTION as briefed and also for CASCADE / SET NULL).
   Otherwise the delete fails or destroys them. RETAIN_ROWS then asserts that they still exist.
3. **Other subjects under a NOT NULL CASCADE FK** (e.g. `referrer_id NOT NULL ON DELETE CASCADE`)
   give CASCADE_DESTROYS_RETAINED, because deleting the subject would delete another customer.
   NOT NULL + RESTRICT gives NOT_NULL_REFERENCE. REFERENCING_OTHER_SUBJECT references are nullified
   even when the subject row itself is retained (ANONYMIZE/RETAIN root).
4. **SHARED rows:** a nullable FK to a deleted parent is nullified, so the shared row survives.
   NOT NULL + RESTRICT/NO ACTION gives RESTRICT_FROM_RETAINED (an acknowledgement could not make
   the delete succeed). NOT NULL + CASCADE is stated in the SHARED_RECORD message ("deleting their
   parent rows cascades to them"). **Acknowledging SHARED_RECORD therefore lets PostgreSQL delete
   the shared row**, e.g. demo message 402 by customer 77 on customer 42's ticket. Proposal:
   decide whether acknowledgement should mean "accept the cascade" or whether this case should
   be non-acknowledgeable.
5. **UNCONFIRMED_PII vs RETAINED_WITH_PII:** UNCONFIRMED_PII (WARNING) is raised for unconfirmed
   suggestions on ANONYMIZE tables, because those columns are not scrubbed. RETAINED_WITH_PII
   (WARNING) is raised for RETAIN tables with any PII, confirmed or not, for ANONYMIZE PII columns
   that cannot be scrubbed (part of the PK, or NOT NULL with strategy NULLIFY; they are excluded
   from `target_columns`), and for PSEUDONYMIZE columns (pseudonymization is not anonymization,
   risks §2.9).
6. **OPAQUE_ARTIFACT** (BLOCKING, acknowledgeable) also covers: an EXTERNAL artifact without an
   `EXTERNAL_TASK` handler; a registered artifact with rows of the subject but no usable strategy
   (NONE, RECOMPUTE_SQL without SQL, REFRESH_MATVIEW on a non-matview); AGGREGATE lineage into a
   key-addressable artifact; an affected artifact that is missing from the catalog.
7. **UNBREAKABLE_CYCLE** is also used for derived-artifact dependency cycles
   (`artifact_order` → `ARTIFACT_CYCLE`). A dedicated `ARTIFACT_CYCLE` code may be clearer.
8. **Root-table protection:** a non-root row of the subject-root table that discovery marked
   OWNED is treated as REFERENCING_OTHER_SUBJECT and is never deleted.
9. **Refresh propagation:** maintainable artifacts (matviews, RECOMPUTE_SQL tables) reachable over
   CONFIRMED edges from any table the plan modifies, or from a rebuilt artifact, are refreshed even
   if discovery did not mark them. VIEWs get no step.
10. Plan hash payload = {request_id, schema_fingerprint, record_set_hash, items[all fields + reason],
    conflicts[code, severity, table, resolved]} as briefed. `plan_id` and conflict `message` are not
    hashed, and conflicts are sorted. The approval's `plan_id` is checked separately.
11. Status machine: DRAFT→{BLOCKED, APPROVED, SUPERSEDED}, BLOCKED→{DRAFT, SUPERSEDED},
    APPROVED→{EXECUTED, FAILED, SUPERSEDED}, EXECUTED→{VERIFIED, FAILED}. VERIFIED, SUPERSEDED and
    FAILED are terminal.

---
## Lead decision (2026-11-04)
- Executor appends `EXECUTION_COMMITTED` itself (same transaction): ACCEPTED. The service must not append a second one. Executor result status COMMITTED (LIVE) / ROLLED_BACK (DRY_RUN): ACCEPTED, and the service persists the final status after its commit/rollback actually happens.
- `plan_hasher=compute_plan_hash` is always passed by the service: ACCEPTED.
- CR §1 `predicate_row_counts`: ACCEPTED. The **service** computes them with `TargetReader.count_by_columns` for predicate keys.
- CR §2/§3 (`recompute_sql`, `pseudonym_columns` kwargs): ACCEPTED as kwargs. No new PlanItem fields (the SQL hash is already bound into the hashed reason).
- CR §5, §6: ACCEPTED.
- Policy §1 AT-11: **reworded.** "order_line = ANONYMIZE under deleted purchase_order → CASCADE_DESTROYS_RETAINED".
- Policy §3, §4, §5: ACCEPTED.
- **Policy §2 (SAFETY), CHANGED: SHARED rows must never be destroyed, not even after acknowledgement.**
  "Never delete unrelated customer records" is a hard rule. Therefore:
  (a) If a SHARED row's link into the subject's record set is a **nullable** FK, the planner emits `NULLIFY_REFERENCE` (detach) on that FK, **ordered before** the parent delete, plus a `SHARED_RECORD` WARNING. The shared row survives.
  (b) If the link is NOT NULL (it would cascade or be restricted), `SHARED_RECORD` is BLOCKING and **not acknowledgeable** (removed from the acknowledgeable set). Resolving it needs a policy/schema decision, not a click.
  (c) Any row that an `ON DELETE CASCADE` would remove but that is not OWNED is a BLOCKING conflict (`CASCADE_DESTROYS_RETAINED`, message "cascade would delete a record not owned by the subject").
  (d) The executor adds a safety assertion: before each parent `DELETE_ROWS`, count the child rows on every CASCADE FK that reference the batch and require that count == the planned `EXPECT_CASCADE` total for that FK, otherwise `RowCountMismatch`. This catches unplanned cascades from concurrent inserts as well.
