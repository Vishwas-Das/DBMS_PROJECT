# Change request frontend-engineer-1: API gaps found while building the Stage 12 UI

Status: OPEN, awaiting Lead review. No contract files were edited. The frontend (`frontend/**`) is
built strictly against `docs/openapi.json` at HEAD 42d0bde and does **not** work around any of the
gaps below. Where data is missing, it shows an explicit empty state that names the gap.

Priorities: **P1** limits a brief requirement. **P2** is a correctness or typing improvement. **P3** is cosmetic or doc-only.

## 1. (P1) No read endpoint for executions
`ExecutionOut` (per-step results, timings, `error`, `evidence_id`) is only returned in the response
of `POST /api/erasure-plans/{plan_id}/execute`. After a page reload, or for another operator, the
Execution Monitor cannot show past dry runs or LIVE runs. The UI keeps the results of execute calls
made in the current browser session (TanStack mutation cache) and says so on the page.
**Proposal:** add `GET /api/erasure-plans/{plan_id}/executions` → `list[ExecutionOut]` (newest first),
and optionally `latest_execution_id: UUID | None` on `ErasureRequestOut`.

## 2. (P1) No read endpoint for the discovery result
`DiscoveryOut` (records_by_role, affected_artifacts, external_dependencies, warnings, record_set_hash)
is only returned by `POST /api/discovery/{request_id}`. It cannot be shown again without re-running
discovery, which appends audit events.
**Proposal:** `GET /api/discovery/{request_id}` → latest `DiscoveryOut` (404 if none).

## 3. (P1) No way to confirm or reject INFERRED edges
CLAUDE.md rule 5 and architecture §4 (`/lineage/suggestions`) require a human to confirm `INFERRED`
edges. `module_contracts.md` §9 had `PATCH /lineage-edges/{id}`, but CR lead-1 §4 did not carry it
over to `/api`. The Lineage Explorer shows SUGGESTED edges (orange, dotted, labelled "(suggested)"),
but it cannot act on them.
**Proposal:** `POST /api/lineage/edges/{edge_id}/status` with body `{status: "CONFIRMED" | "REJECTED"}`
(token required, only `INFERRED` edges, audited) → `GraphEdgeOut`.

## 4. (P2) Acknowledge `note` is accepted but dropped
`POST /api/erasure-plans/{plan_id}/conflicts/{conflict_id}/acknowledge` accepts a
`ConflictAcknowledge {note}` body, but `app.py` never passes `body` to
`s.acknowledge_conflict(plan_id, conflict_id, actor)`. The UI sends no note for now.
**Proposal:** pass the note through to the service and the audit payload (it is free text, so it
should be length-limited and kept out of evidence), or remove the body from the contract.
`PlanConflictOut` could also expose `acknowledged_by` / `acknowledged_at`. Today the only signal
is `resolution != null`.

## 5. (P2) `residual_hits` is untyped
`VerificationOut.residual_hits: list[dict[str, Any]]` generates `{[key: string]: unknown}[]`. The UI
needs table, column and count, so it reads `schema_name|schema`, `table_name|table`,
`column_name|column` and `count|hits|row_count` defensively and also shows the raw object.
**Proposal:** a `ResidualHitOut {schema_name, table_name, column_name, hit_count: int}` model
(counts only, never values).

## 6. (P2) Map-typed fields lose their enum keys
`PlanOut.counts_by_category: dict[ActionCategory, int]` and `DiscoveryOut.records_by_role` /
`affected_artifacts` are emitted as `additionalProperties` without `propertyNames`, so the
generated TS key type is `string`. The UI casts the keys to `ActionCategory` / `RecordRole` for
labels. That is safe today but not checked by the compiler.
**Proposal (optional):** emit these as objects with one optional property per enum value, or as
arrays of `{category, count}`.

## 7. (P2) `GET /api/customers` returns the raw subject key
`CustomerOut.id` is the customer PK, which is the subject key for root `customer`. The brief
accepts this because the customer search is the request form, and the UI never displays the id,
only sends it once in `POST /api/erasure-requests`. For a stricter design, the endpoint could
return an opaque per-session handle that the server resolves when the request is created.
**Decision needed:** keep as is (documented in risks) or switch to a handle.

## 8. (P3) Plan history
Only `ErasureRequestOut.current_plan_id` is exposed, so SUPERSEDED plan versions are not
browsable. **Proposal:** `GET /api/erasure-requests/{id}/plans` → `list[PlanOut]` (summary only).

## 9. (P3) Docs out of sync with CR lead-1
`module_contracts.md` §9 and §10 still describe `/api/v1` and the old paths (`/plans/{id}/approve`,
`/snapshots/{id}/graph`, `RecordGraphOut`, and so on). The UI follows CR lead-1 §4 and
`docs/openapi.json`. **Proposal:** update §9 and §10 to the `/api` paths.

## 10. (P3) Questions
- `POST /api/audit/verify-chain` has no `X-LAE-Token` dependency, unlike the other POSTs. Is that
  intended because the call is read-only? The client sends the token on every POST regardless, and
  this does no harm.
- When the database is down, does `GET /api/health` return 200 with `status: "degraded"` and
  `database: false`, or an error envelope? The UI handles both.
- Evidence `download` / `leaves` have an empty 200 schema (`{}`). The UI treats them as opaque
  attachments (Blob), which is fine. A declared schema would only help documentation.

---
## Lead decision (2026-11-04)
Accepted, to be added during service integration (stage 11), with the OpenAPI re-exported afterwards:
- `GET /api/executions?request_id=` (past runs). `GET /api/discovery/{request_id}` (latest result, no re-run). `POST /api/lineage-edges/{edge_id}/status` (confirm/reject INFERRED edges, token-protected).
- The acknowledge `note` is passed through to the service and the audit payload. `residual_hits` gets a typed `ResidualHitOut`.
- `CustomerOut.id`: kept. It is a synthetic app PK and is required to create a request. Email is masked. Documented in security_analysis.md.
- verify-chain is read-only, so no token (it only appends nothing). Health returns `status="degraded"`, `database=false` when the DB is down (HTTP 200).
- Plan versions list, enum-typed map keys, evidence schema: deferred (P3).
