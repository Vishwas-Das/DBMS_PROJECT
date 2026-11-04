# CR frontend-engineer-2: defects found while integrating the UI with the live API

Author: Frontend Engineer. Date: 2026-11-04. Backend under test: HEAD 57de8e8 (after the security
hardening), dev DB `lae`, started with `uvicorn lae.api.main:app --port 8000`.
All requests below carried a valid `X-LAE-Token` (value omitted). No raw subject keys are quoted.

## 1. (P1) Residual scan fails a correct erasure: homonyms in the root table, pseudonymized columns
Live run: request `2025f31d-d7d7-478c-84e6-4432673c039a`, plan `bbf1e413-…`, LIVE execution
`126a8558-…` COMMITTED, all 11 executable steps APPLIED with matching rowcounts.
`GET /api/verification/2025f31d-…` →

```json
{"check_type": "RESIDUAL_VALUE_SCAN", "passed": false,
 "observed": {"hits": [{"count": 1, "table": "app.customer", "column": "full_name"},
                       {"count": 3, "table": "app.purchase_order", "column": "shipping_name"}],
              "total_hits": 4, "probe_count": 5, "retained_hits": [], "columns_scanned": 26},
 "expected": {"total_hits": 0}}
```

outcome `FAILED`, request status `FAILED`.
- `app.customer.full_name`, 1 hit: the subject row is deleted (ROW_ABSENT passed), so the hit is
  another customer with the same synthetic name. A value scan over the subject-root table cannot
  tell homonyms from residue. Either exclude non-unique quasi-identifiers on the root table, or
  report such hits as "ambiguous" instead of failing.
- `app.purchase_order.shipping_name`, 3 hits: step 2 `ANONYMIZE_ROWS` pseudonymized `shipping_name`
  on these 4 retained rows (RETAINED_WITH_PII warning). The hits are not reported in
  `retained_hits` (it is `[]`), so the plan's retention decision turns into a verification
  failure. Expected: retained / pseudonymized rows counted as `retained_hits` (outcome capped at
  PARTIAL), or the pseudonym must not match the probe.
The residual-to-SUGGESTED feedback skips the root table, so the operator has no follow-up action
for the first hit.

## 2. (P1) `GET /api/discovery/{id}` recomputes discovery instead of returning the stored result
`services/impl.py::latest_discovery` calls `run_discovery(..., audit=False)`. Consequences:
- Before any `POST /api/discovery/{id}`, GET already returns a full result (a new request in
  status RECEIVED showed `records_found: 26`). The UI cannot tell whether discovery has run.
- After a LIVE erasure, `GET /api/discovery/2025f31d-…` → `404 {"error": {"code":
  "SUBJECT_NOT_FOUND", "message": "subject not found in root table", ...}}`, so the discovery that
  the executed plan was based on can no longer be shown.
- Every page view re-executes the discovery queries.
The Lead decision in CR frontend-engineer-1 says "latest result, no re-run". **Proposal:** persist
the `DiscoveryOut` summary (counts and hashes only, no keys) at POST time and return it. Keep 404
NOT_FOUND only for "never run". The UI treats any 404 as "not available" and shows the code and
message.

## 3. (P2) `POST /api/lineage-edges/{id}/status` returns table node ids for COLUMN edges
`POST /api/lineage-edges/2f50347f-0ee8-5334-92f8-13f716716500/status {"status":"CONFIRMED"}` →
`level: "COLUMN", source: "t:app.customer", target: "t:app.marketing_event_log"`. The same edge
in `GET /api/lineage/schema?level=COLUMN` has `c:app.customer.email` →
`c:app.marketing_event_log.email`. `impl.set_edge_status` always builds `t:` ids. The UI is not
affected (it refetches the graph and selects by edge id), but the response contradicts the graph.
Also, re-posting the current status (CONFIRMED → CONFIRMED) is accepted and appends an
`EDGE_STATUS_CHANGED` event with `from == to`. I did this once while diagnosing the bug.
Consider making it a no-op or a 409.

## 4. (P3) Duplicate INFERRED edges for the same column pair
After a LIVE run, the column graph holds two INFERRED SUGGESTED edges
`c:app.customer.email → c:app.marketing_event_log.email`: one from
`name_pattern+sampled_containment` and one from `residual_scan` (`hit_count: 4`). The operator has
to confirm both. **Proposal:** merge the residual evidence into the existing edge's `evidence`
when the same (level, source, target, columns) edge exists.

## 5. (P3) `GET /api/executions?request_id=<unknown>` returns `200 []`
Other request-scoped reads return `404 NOT_FOUND` for an unknown request. The UI copes with both.

## 6. Notes (no change needed)
- With the four-eyes rule, the UI sends the Settings "Approver name" as `X-LAE-Actor` on
  approve and every other call, and the "Executor name" on execute. A 409 whose message mentions
  separation of duties shows an explanation and an "Open Settings" button.
- `GET /api/lineage/schema` 404 before the first discovery is shown as an empty state.

---
## Lead decision (2026-11-04)
1. (P1) Residual false positives from **namesakes**: FIXED. Probes whose value also occurs in another row of the subject-root table are excluded at planning time (`pipeline._exclude_ambiguous_probes`, counted as `ambiguous_probes_excluded` in PLAN_CREATED). Regression: `test_shared_name_does_not_cause_false_residual_failure` (VERIFIED, namesake untouched). The hidden-copy test (unique email) still FAILS as it should. Trade-off documented in limitations.md. Screenshot 05 shows the **pre-fix** run and is kept as evidence of the defect.
2. (P1) GET /api/discovery re-computes read-only (no stored discovery results). The decision text is corrected: it is a read-only recomputation and never writes. 404 after erasure is the correct behaviour (the subject no longer exists).
3. (P2) Edge status: idempotent (no-op, no audit event, when unchanged). COLUMN edges return `c:` node ids. FIXED.
4. (P3) Duplicate residual suggestions for an already-inferred column pair are skipped. FIXED.
5. (P3) `GET /api/executions?request_id=<unknown>` gives 404. FIXED.
Playwright not installed: correct call (needs user approval). The dependency-free Edge/CDP smoke script is accepted.
