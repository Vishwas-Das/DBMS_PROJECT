# Testing

How the prototype is tested, how to run each suite, what exists (counts re-run on 2026-11-25), how the acceptance tests AT-01…AT-24 map to real tests, and what the tests do **not** prove.

---

## 1. Strategy

| Level | Marker | Database | What it checks | Where |
|---|---|---|---|---|
| Unit | `unit` | none | Pure domain logic through **in-memory fakes of the ports**: `FakeCatalogReader` / `FakeTargetReader` (`tests/unit/discovery/conftest.py`), and an FK-enforcing `FakeDB` that acts as an ordering oracle (`tests/unit/planner/erasure_harness.py`, used by planner, executor and verification tests). Also golden vectors for canonical JSON, HMAC, chain, Merkle, signatures and evidence (`tests/unit/audit/vectors/`), and Hypothesis property tests (`tests/unit/executor/test_properties.py`, `tests/unit/lineage/test_properties.py`) | `backend/tests/unit/**` |
| Integration | `integration` | **real PostgreSQL 16 + pgvector + pgcrypto** | Migrations, constraints and triggers, adapters (`PgCatalogReader`, `PgTargetReader/Writer`, `PgAuditSink`), repositories, the generator, discovery against generator ground truth, verification checks and the residual scan on real data, DB-backed evidence | `backend/tests/integration/{db,discovery,verification}` |
| End-to-end (API) | `integration` (see note) | real PostgreSQL | The full HTTP flow through `fastapi.testclient.TestClient` against `create_app(LaeServices(rt))`. Every erasure claim is re-checked with **direct SQL**, independently of the engine's own report | `backend/tests/integration/services/test_api_e2e.py` |
| Benchmarks | `bench` | real PostgreSQL | Excluded by default (`addopts = -m "not bench"`). No `bench` tests exist at HEAD | – |

**Note on markers:** the API tests in `tests/integration/services/` use `integration`. `tests/e2e/` (64 security tests) uses `e2e` and runs on `lae_test_qa`.

**Rule from CLAUDE.md: don't mock SQL semantics.** The unit fakes model FK behaviour (RESTRICT, NO ACTION, CASCADE, SET NULL), but anything that depends on real SQL (`EXCEPT ALL`, pgcrypto `hmac`, `pg_get_viewdef` text, `FOR UPDATE`, SERIALIZABLE errors) is tested only in the integration suites.

## 2. Test databases

- Fixtures: `backend/tests/conftest.py` reads `LAE_TEST_DATABASE_URL` (default `postgresql+psycopg://lae_owner:lae_owner_dev@127.0.0.1:5433/lae_test`) and **skips** integration tests with an explicit reason if PostgreSQL is unreachable.
- `backend/tests/integration/conftest.py: migrated_url` only accepts `lae_test` or `lae_test_<suffix>`. `migrated_db` **drops and recreates** the `lae` and `app` schemas, then runs `alembic upgrade head`. `seeded_db` loads the generator dataset (default `tiny`, seed 7) and re-seeds only when (scale, seed) changes or a test calls `mark_dataset_dirty`.
- Per-workstream databases exist on the local server: `lae_test_lead` (db, services), `lae_test_disc` (discovery), `lae_test_verif` (verification), plus `lae_test_erasure`, `lae_test_qa` and `lae_test_bench`. `db/init/01_init.sql` creates all of them on a fresh volume, and `scripts/create_test_dbs.py` on an existing one.
- The session-scoped fixtures mean that **all integration tests of one pytest session must use the same database URL**, so run each directory against its own DB.

## 3. How to run

From `backend/`, with the venv:

```powershell
# unit (no DB)
..\.venv\Scripts\python -m pytest tests -m unit -q

# integration, one workstream per DB
$env:LAE_TEST_DATABASE_URL="postgresql+psycopg://lae_owner:lae_owner_dev@127.0.0.1:5433/lae_test_lead"
..\.venv\Scripts\python -m pytest tests\integration\db tests\integration\services -m integration -q
$env:LAE_TEST_DATABASE_URL="postgresql+psycopg://lae_owner:lae_owner_dev@127.0.0.1:5433/lae_test_disc"
..\.venv\Scripts\python -m pytest tests\integration\discovery -m integration -q
$env:LAE_TEST_DATABASE_URL="postgresql+psycopg://lae_owner:lae_owner_dev@127.0.0.1:5433/lae_test_verif"
..\.venv\Scripts\python -m pytest tests\integration\verification -m integration -q

# collection only (what was done for the counts below)
..\.venv\Scripts\python -m pytest tests\unit\planner --collect-only -q
```

Quality gates (CLAUDE.md): `ruff check backend`, `ruff format --check backend`, `mypy backend\src\lae`, `lint-imports --config backend\pyproject.toml`. `scripts\check.ps1` runs everything; results are in [final_repository_audit.md](final_repository_audit.md).

## 4. Inventory (real counts)

Counts are from the full run of `scripts\check.ps1` on 2026-11-25. Parametrized cases count individually.

| Directory | DB used for collection | Collected tests | Main files |
|---|---|---|---|
| `tests/unit/api` | – | 7 | `test_app.py` (token auth, error envelopes, no value echo, OpenAPI paths) |
| `tests/unit/audit` | – | 311 | canonical, hmac_keys, hashchain, merkle, signing, evidence, leaves, envelope, anchor, golden vectors |
| `tests/unit/contracts` | – | 10 | `test_contracts.py` |
| `tests/unit/db` | – | 14 | `test_sql_validator.py` (`assert_single_select`, added in `57de8e8`) |
| `tests/unit/discovery` | – | 215 | catalog, foreign_keys, sql_lineage, sql_lineage_matrix, records, residual_probes, artifacts/inference/pii |
| `tests/unit/executor` | – | 35 | `test_executor.py`, `test_properties.py` (Hypothesis) |
| `tests/unit/lineage` | – | 75 | graph, record_graph, roles, api, properties |
| `tests/unit/planner` | – | 135 | build_plan, artifacts, hashing_state |
| `tests/unit/verification` | – | 105 | engine, metrics, outcome |
| **Unit total** | | **907** | |
| `tests/integration/db` | `lae_test` | 184 | migrations, constraints, engine, catalog reader, target, audit sink, repositories, generator |
| `tests/integration/services` | `lae_test_lead` | 20 | `test_api_e2e.py` (9), `test_security_hardening.py` (11, including second snapshot after a schema change, and a plan superseded before execution) |
| `tests/integration/discovery` | `lae_test_disc` | 42 | catalog, FK, SQL lineage, inference/edge cases, records vs ground truth, residual probes |
| `tests/integration/verification` | `lae_test_verif` | 24 | checks, outcome, residual, audit evidence |
| **Integration total** | | **270** | |
| **All backend** | | **1 241** | (plus 73 frontend Vitest tests) |
| `tests/e2e` | `lae_test_qa` | 64 | `test_security_api.py`, `test_security_db.py` (marker `e2e`) |

## 5. Acceptance tests AT-01…AT-24 → covering tests

Source: `docs/development_plan.md` §6, with AT-11 reworded by the Lead (CR erasure-engine-engineer-1). The mapping was found with grep. Only AT-10, AT-11, AT-12, AT-15, AT-20 and AT-21 are named explicitly in test code. The rest are mapped by content.

Status meanings:
- **COVERED**: the pass condition is asserted as worded.
- **PARTIAL**: part of it is asserted, or it is asserted only on fakes.
- **DIVERGES**: the code behaves differently from the wording, and a test pins the actual behaviour.
- **NOT COVERED**: no test asserts it.

| AT | Scenario | Covering tests (file: function) | Status |
|---|---|---|---|
| AT-01 | Snapshot of demo `app` | `integration/discovery/test_catalog_integration.py`: `test_all_relations_and_kinds`, `test_primary_keys_composite_and_matviews_without_pk`, `test_fingerprint_stable_across_reads_and_analyze`, `test_fingerprint_changes_on_alter_and_drift_reports_it`. `integration/db/test_catalog_reader.py: test_lists_every_relation_with_kind_and_pk` | COVERED. The live catalog reports **17** relations (15 tables, including the CTAS table `customer_segment`, plus 2 matviews). The "15" in the AT wording predates CR lead-1 §5 (`document`, `customer_profile_cache`) |
| AT-02 | FK discovery | `integration/discovery/test_foreign_keys_integration.py`: `test_every_declared_fk_has_a_table_edge`, `test_column_edges_one_per_pair_in_order`, `test_edge_attributes_and_evidence`. `integration/db/test_catalog_reader.py: test_foreign_keys_composite_order_and_actions`. `integration/discovery/test_catalog_integration.py: test_composite_fk_order_from_reader`. Unit: `test_foreign_keys.py: test_composite_fk_keeps_column_order` | COVERED |
| AT-03 | SQLGlot `customer_ltv` | `integration/discovery/test_sql_lineage_integration.py: test_customer_ltv_key_preserving_via_customer_id` (and `..._without_extra_keys_is_aggregate`). Unit: `test_sql_lineage.py: test_demo_artifacts_chain` | COVERED (column is `amount_cents`, not `amount`) |
| AT-04 | SQLGlot `daily_revenue` | `integration/discovery/test_sql_lineage_integration.py: test_daily_revenue_aggregate`. Unit: `test_sql_lineage.py: test_demo_artifacts_chain` | COVERED |
| AT-05 | Unparseable SQL / UDF → OPAQUE, no exception | Unit: `test_sql_lineage.py: test_never_raises_on_arbitrary_sql`, `test_internal_errors_never_escape`, `test_path_explosion_is_opaque`. `test_sql_lineage_matrix.py: test_classification_matrix`, `test_warnings_never_echo_sql_literals` | COVERED |
| AT-06 | Inference `marketing_event_log.email` ≥ 0.95, not traversed | `integration/discovery/test_inference_and_edge_cases.py: test_hidden_copy_suggested_at_lower_threshold_and_never_traversed`, `test_inference_suggests_only_and_skips_known_edges`. Unit: `test_artifacts_inference_pii.py: test_inferred_edges_are_suggested_with_confidence` | **DIVERGES**. At default θ = 0.95 there is no suggestion (containment 17/19). It is suggested with confidence 0.805 at θ = 0.8 and is never traversed. "Not traversed" is COVERED |
| AT-07 | Record discovery = ground truth, no `product` | `integration/discovery/test_records_ground_truth.py`: `test_primary_subject_roles`, `test_all_tiny_subjects_and_random_sample`, `test_small_scale_known_and_random_subjects`, `test_default_scale_random_subjects` | COVERED (subject 7/42 depending on scale) |
| AT-08 | Other subjects (REFERENCING_OTHER_SUBJECT, NULLIFY `referrer_id`, survives) | Roles: `test_records_ground_truth.py: test_primary_subject_roles`. Plan: unit `test_build_plan.py: test_other_subjects_are_never_deleted`. Survival after execution: `integration/services/test_api_e2e.py: test_full_erasure_happy_path_checked_against_the_database` (asserts no other customer was deleted, but its subject has no referrals) | PARTIAL (no real-DB execution of a referring subject is asserted) |
| AT-09 | Shared record → `SHARED_RECORD` BLOCKING | `integration/services/test_api_e2e.py: test_shared_records_block_live_execution`. `test_records_ground_truth.py: test_primary_subject_roles`, `test_secondary_subject_shares_the_same_messages`. Unit: `test_build_plan.py: test_not_null_shared_record_is_blocking_and_cascade_flagged`. `test_hashing_state.py: test_shared_record_can_never_be_acknowledged` | COVERED |
| AT-10 | Ordering (children before parents, derived after base in `artifact_order`) | Unit: `test_properties.py: test_random_plans_are_ordered_and_execute_safely` (explicit "AT-10"). `test_build_plan.py: test_deletes_children_before_parents_in_shop`. `test_artifacts.py: test_derived_steps_follow_base_steps_in_artifact_order`. `lineage/test_properties.py: test_dag_deletion_order_respects_every_edge` | COVERED (on fakes) |
| AT-11 | (reworded) `order_line = ANONYMIZE` under deleted orders → `CASCADE_DESTROYS_RETAINED`, BLOCKED | Unit: `test_build_plan.py: test_anonymize_cascade_child_conflict` ("AT-11 variant") | PARTIAL (fakes only, no real-DB test) |
| AT-12 | Plan hash determinism and sensitivity | Unit: `test_hashing_state.py`: `test_same_inputs_same_hash_and_bytes`, `test_hash_is_stable_across_processes`, `test_any_item_field_change_changes_hash`, `test_plan_level_changes_change_hash`, `test_conflict_changes_change_hash` | COVERED |
| AT-13 | Wrong hash → 409 `PLAN_HASH_MISMATCH`, unapproved execute → 409 `INVALID_STATE` | `integration/services/test_api_e2e.py: test_full_erasure_happy_path_checked_against_the_database` (wrong hash). `test_security_hardening.py: test_four_eyes_and_replan_after_stale` (409 when the approver executes) | COVERED (`e2e/test_security_api.py` asserts unapproved execution is refused) |
| AT-14 | Dry run: correct counts, data byte-identical | `test_api_e2e.py: test_full_erasure_happy_path...` (row **counts** of 14 tables unchanged). Unit: `test_executor.py: test_dry_run_performs_identical_operations_and_changes_nothing` | PARTIAL (counts, not checksums, on the real DB) |
| AT-15 | Injected failure → full rollback + rollback event | Unit: `test_executor.py: test_injected_failure_at_any_step_rolls_back_fully` ("AT-15 on the fake"), `test_dml_rowcount_mismatch_rolls_back_everything`. Real-DB rollback on drift: `test_api_e2e.py: test_drift_between_approval_and_execution_is_detected` | PARTIAL. Real-DB rollback is asserted in `e2e/test_security_db.py`, but no test asserts the audit event. A failed LIVE run appends `EXECUTION_FAILED` (not `EXECUTION_ROLLED_BACK`, which is used for DRY_RUN) |
| AT-16 | Drift → `STALE_PLAN`, nothing changes | `test_api_e2e.py: test_drift_between_approval_and_execution_is_detected` (late `customer_address` insert). Unit: `test_executor.py: test_new_order_after_planning_is_stale_and_nothing_is_written`, `test_schema_change_is_stale`, `test_unplanned_cascade_row_inserted_after_planning_rolls_back` | COVERED (the real-DB test uses an address, the unit test uses an order) |
| AT-17 | Second LIVE → 409 `ALREADY_EXECUTED` | `test_api_e2e.py: test_full_erasure_happy_path...` asserts only `status_code == 409` | **DIVERGES**. The real API returns 409 `INVALID_STATE` ("only an APPROVED plan can be executed LIVE"), because the plan status is no longer APPROVED |
| AT-18 | Verification passes. `DERIVED_CONSISTENT` for ltv, daily_revenue, segment | `test_api_e2e.py: test_full_erasure_happy_path...` (all checks passed, outcome `PARTIAL` due to the external artifact). `integration/verification/test_outcome_real.py: test_verified`. `test_checks_real.py: test_derived_consistent_matview_refresh_real` | COVERED (checks pass. The outcome is VERIFIED only without the EXTERNAL artifact, `test_verified_outcome_purges_raw_subject_data`) |
| AT-19 | Residual scan FAILED without confirmed edge, PASSED after confirm + re-plan | First half: `test_api_e2e.py: test_hidden_copies_fail_verification_and_are_suggested`. `integration/verification/test_residual_real.py: test_fk_only_erasure_leaves_hidden_copies`, `test_lower_trim_is_needed_for_the_variants`. Second half: `test_residual_real.py: test_erasing_hidden_copies_passes_and_verifies` (uses a hand-built plan that targets all three spellings, not the confirm-and-re-plan workflow) | PARTIAL. The confirm → re-plan → PASSED workflow is not tested. Confirmed-edge traversal matches exact copies only (`test_inference_and_edge_cases.py: test_confirmed_inferred_email_edge_finds_exact_copies_by_predicate`), so subjects with case variants still fail |
| AT-20 | Embeddings deleted, `EMBEDDING_ABSENT` passes | `integration/verification/test_checks_real.py: test_embedding_absent_real`. `test_audit_evidence_real.py` (mentions AT-20) | COVERED (key-based check only) |
| AT-21 | Audit append-only, chain valid, tamper detected at exact seq | `integration/db/test_constraints.py: test_audit_event_is_append_only`. `integration/db/test_audit_sink.py: test_tampering_is_detected_at_exact_seq`, `test_concurrent_appends_are_serialized`. `integration/verification/test_audit_evidence_real.py: test_tampered_payload_fails_offline_at_exact_seq`, `test_tampered_envelope_column_fails_offline_at_exact_seq`. `test_api_e2e.py: test_audit_events_cannot_be_modified` | COVERED |
| AT-22 | Merkle + signature golden vectors, proofs, bit flip | Unit: `test_golden_vectors.py` (all), `test_merkle.py: test_all_proofs_for_sizes_0_to_17`, `test_root_matches_rfc6962_reference`. `test_evidence.py: test_flipped_signature_bit`. `test_signing.py: test_tampered_signature_fails` | COVERED |
| AT-23 | `lae verify-evidence` with no DB. Modified bundle fails | `integration/verification/test_audit_evidence_real.py: test_db_backed_evidence_verifies_offline`, `test_forged_bundle_and_wrong_key_fail`. `test_api_e2e.py` (calls `verify_evidence_offline`). Unit: `test_golden_vectors.py: test_committed_vector_verifies_offline` | PARTIAL. The **CLI command** is not tested. The function is. The CLI was exercised by hand in this pass (exit 0 valid, exit 1 tampered) |
| AT-24 | No PII leakage (lae schema after purge, API responses, logs) | `test_security_hardening.py: test_four_eyes_and_replan_after_stale` (0 raw keys in a superseded plan). `test_api_e2e.py: test_no_raw_pii_in_responses_or_logs` (responses, captured logs and `audit_event.payload_canonical` contain no raw e-mail or phone). `test_verified_outcome_purges_raw_subject_data` (raw subject key and `plan_item.record_keys` are NULL after purge). `integration/discovery/test_residual_probes_integration.py: test_primary_probes_are_hmacs_only` | COVERED (`e2e/test_security_db.py` scans the `lae` tables after purge) |

No AT is completely **NOT COVERED**. Several are only PARTIAL, and two DIVERGE from their wording (see the Status column).

## 6. What coverage does not prove

- **Line coverage** was measured once for the unit suite (development_progress.md §1) and was not re-measured in the 2026-11-25 audit.
- **Passing tests do not prove completeness of discovery.** Recall is measured against the generator's own model of the data. Copies the generator does not create (free-text mentions, JSON-embedded values, application-maintained denormalizations, trigger side effects) are invisible to both the oracle and the tests.
- **Fakes encode assumptions.** `FakeDB` implements FK semantics as the planner author understands them. Ordering and atomicity are proved against that oracle, plus a small number of real-DB flows, not against every PostgreSQL behaviour (deferred constraints, triggers, `SET DEFAULT` with defaults, partitioned tables).
- **One schema, small data.** The integration suites mostly use the `tiny` preset (20 customers), and some use `small` and `default`. Behaviour at scale, under concurrent writers, or on other schemas is not established.
- **No concurrency proof.** Drift detection is tested by inserting *between* approval and execution. True concurrent writers during the SERIALIZABLE transaction, and serialization-failure handling under load, are tested only for error translation (`test_engine.py: test_serialization_failure_is_translated`).
- **Security tests are negative samples, not proofs.** "No raw e-mail in responses/logs" is checked for a few subjects and endpoints. It does not prove that no code path ever leaks a value.
- **Cryptographic tests check the use of the primitives, not the primitives.** Golden vectors pin LAE's formats. The security of SHA-256, HMAC and Ed25519 is assumed.
- **Physical erasure is not tested.** Dead tuples, WAL, backups and replicas are out of scope.
- **Legal sufficiency is not tested.** A `VERIFIED` outcome means the engine's checks passed. It does not mean any law is satisfied.
