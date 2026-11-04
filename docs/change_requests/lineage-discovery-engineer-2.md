# Change request lineage-discovery-engineer-2: findings from discovery integration on real PostgreSQL

Status: OPEN, awaiting Lead review. No contract or adapter files were edited.
Scope: round 2 of stages 3–5. `backend/tests/integration/discovery/**` (42 tests) runs
`lae.discovery` through `PgCatalogReader`, `PgTargetReader`, `ArtifactRepo` and `PolicyRepo` on
the seeded `app` schema (tiny/7, small/1, default/42) in database `lae_test_disc`.

**Result:** no bugs were found in `lae.discovery` or in the DB adapters. Record discovery has
precision = recall = F1 = 1 against the generator's ground truth (owned + key-preserving derived
rows) for every tiny subject (20/20), 20 small-scale subjects and 25 default-scale subjects.
SHARED and REFERENCING_OTHER_SUBJECT sets match the truth exactly. The items below are test
infrastructure, a recall limitation, and one semantic divergence that is not exercised yet.

## 1. Test infrastructure: the shared DB guard only accepts `lae_test` (Database Engineer)
`backend/tests/integration/conftest.py` line 36 (`ALLOWED_TEST_DATABASE = "lae_test"`) and line 93
(`if make_url(url).database != ALLOWED_TEST_DATABASE: pytest.fail(...)`) reject the
per-workstream databases that db/init/01_init.sql now creates.
Repro: `LAE_TEST_DATABASE_URL=postgresql+psycopg://lae_owner:...@localhost:5433/lae_test_disc
pytest backend/tests/integration/db -m integration` fails with "integration fixtures only run
against the 'lae_test' database".
Workaround (my path only): `backend/tests/integration/discovery/conftest.py` overrides the
session fixture `migrated_url` with an allow-list of exactly `{"lae_test", "lae_test_disc"}`.
**Proposal:** the shared guard accepts `lae_test` and `lae_test_<suffix>` (`^lae_test(_[a-z]+)?$`).
After that I will delete my override.

## 2. Inference misses case/whitespace variants of e-mail copies at the default θ (limitation)
`infer_relationships` uses `CatalogReader.count_contained`, which compares exact text
(`{c}::text = ANY(...)`). On tiny/7, `marketing_event_log.email → customer.email` gives 17/19
distinct sampled values contained (the seeded `"  EMAIL\t"` upper-case and title-case copies do not
match). Confidence = 17/19 × 0.9 = 0.805, below θ = 0.95, so **no suggestion at the default
threshold**. At θ = 0.8 it is suggested (SUGGESTED, 0.805). Even if a human confirms it, traversal
matches exact copies only (3 of the subject's 5). Pinned in
`test_inference_and_edge_cases.py`.
This is consistent with the design: the residual scan (HMAC LOWER_TRIM probes) finds all 5. The
integration test confirms `hmac_match_count` = 5 (LOWER_TRIM) and 3 (EXACT) with the probe
digests. **Options:**
- (a) Recommended for v1: document it in risks_and_limitations.md ("inference containment is
  exact-text; normalised copies are found only by the residual scan").
- (b) Contract change: `count_contained(child, parent, sample, *, normalization=EXACT)`. Inference
  would use LOWER_TRIM for the `email` pattern, and traversal of a confirmed e-mail edge would need
  a matching normalised predicate in `select_by_columns`. That touches the ports, the adapters and
  the planner's predicate keys, so I don't recommend it for v1.

## 3. SHARED for rows whose referenced parent is ownerless (semantic divergence, not seeded)
Accepted rule (CR-1 §6): a reached row is SHARED if one of its references into a
subject-reachable table points at a row outside {SUBJECT_ROOT, OWNED}. A message written by the
subject on an **anonymous** ticket (`support_ticket.customer_id IS NULL`) therefore becomes
**SHARED**. The generator's FORMAT (`build_ground_truth`: author = c and ticket owner None ⇒
`owned`) and the wording of architecture.md §8 rule 3 ("referencing another subject's root row")
would make it **OWNED**. The generator never creates this case (messages on anonymous tickets
always have author NULL), so the metrics are unaffected. Repro (rolled back):
`test_message_on_anonymous_ticket_is_shared_documented_divergence`.
Safety: SHARED is the conservative direction (MANUAL_REVIEW, never destroyed, CR erasure-1).
**Options:**
- (a) Recommended for v1: keep the conservative rule. Align the wording of architecture §8 rule 3
  and the generator FORMAT ("references a row outside the subject's set").
- (b) Exact attribution: for each outside reference, read the referenced parent row (read-only,
  upstream *lookup*, never a record) and mark SHARED only if it transitively references another
  root row. That costs extra queries per outside reference, and the code is mine to change if the
  Lead prefers it.

## 4. Service-layer requirements confirmed on the real viewdefs (Service / Lead)
- **`extra_keys` is mandatory for exact results.** Without it, `customer_ltv` (real viewdef) is
  AGGREGATE. Because no key propagates, the CTAS `customer_segment` then becomes **OPAQUE**
  ("values of app.customer_ltv reach app.customer_segment without a surviving key column"), which
  makes the plan MANUAL_REVIEW. With `extra_keys` = FK child columns whose parent is the root or
  FK-reachable from it, `customer_ltv` is KEY_PRESERVING via `purchase_order.customer_id` and
  `customer_segment` is KEY_PRESERVING via `customer_ltv.customer_id`. The fixture helper
  `subject_chain_keys` in `tests/integration/discovery/conftest.py` is a reference
  implementation.
- Call `extract_artifacts_lineage` with **all** inspectable artifacts at once, so that key
  propagation along artifact chains reaches its fixpoint.
- Pass FK **and** artifact edges as `exclude` to `infer_relationships`. Even then it suggests
  `customer.id → customer_segment.customer_id` (containment 1, confidence 0.999), because the
  CTAS has no FK and its artifact edge comes from `customer_ltv`. That is harmless (SUGGESTED), but
  confirming it would add a redundant path. Proposal (additive, my code): `infer_relationships(...,
  *, skip_targets=())` so the service can exclude registered artifact targets. I need approval
  before adding it.

## 5. Observed real `pg_get_viewdef(oid, true)` text (PostgreSQL 16.15) and SQLGlot 30.29.0
- `customer_ltv`: aliases are kept (`o.`, `p.`). Formatting is a leading space, newline +
  4-space indents, and a trailing `;`. The registered definition is byte-identical to the viewdef.
- `daily_revenue`: single-table query. PostgreSQL **drops the `p.` qualifier** (`paid_at`,
  `amount_cents`), rewrites `'UTC'` as `'UTC'::text`, and wraps the GROUP BY expression in an
  **extra pair of parentheses**: `GROUP BY ((paid_at AT TIME ZONE 'UTC'::text)::date)`. SQLGlot
  30.29.0 keeps that `exp.Paren` in the GROUP BY. Discovery strips parentheses, casts and aliases (`_strip`) before it matches
  grouped expressions, so `day` is recognised as grouped and the result is AGGREGATE
  (`payment.paid_at → day` EXPRESSION, `amount_cents → revenue_cents` AGGREGATE). There were no
  warnings for the three definitions.

---
## Lead decision (2026-11-04)
1. Guard widened by the Lead in `tests/integration/conftest.py` (`lae_test` or `lae_test_<suffix>`). Your local override can be removed.
2. ACCEPTED as a documented limitation. The Lead implemented **residual-hit feedback**: hits become SUGGESTED `INFERRED` edges (`evidence: residual_scan`), carried across snapshots. A human confirms them. Exact-match traversal then deletes the exact copies, and case/whitespace variants remain detectable only by the scan (honest PARTIAL/FAILED).
3. Keep the conservative SHARED rule. The docs are aligned (architecture §8 wording updated in docs/algorithms.md).
4. `extra_keys`: implemented in `lae.services.pipeline._extra_keys` (subject chain only). `skip_targets` deferred: the spurious `customer.id → customer_segment` suggestion stays SUGGESTED (never traversed) and can be rejected in the UI.
