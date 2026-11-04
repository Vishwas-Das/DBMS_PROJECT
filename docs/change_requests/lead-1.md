# CR lead-1: Mission-brief alignment (2026-11-04)

Author: Lead Architect. Status: **ACCEPTED, applied before the database/discovery/planner/verification agents start.**
Reason: the implementation mission brief adds requirements that the approved contracts did not cover. These are additive extensions. The architecture is **not** replaced.

## 1. Action categories (brief §8)
The brief's vocabulary is a **category** shown to users. The fine-grained `PlanAction` remains the executable contract.

| Category (`ActionCategory`) | PlanAction(s) | Meaning |
|---|---|---|
| `DELETE` | `DELETE_ROWS`, `EXPECT_CASCADE`, `DELETE_DERIVED_BY_KEY` | rows removed |
| `INVALIDATE` | `INVALIDATE_CACHE` (new), `NULLIFY_REFERENCE` | cache entries dropped / references to the subject removed from rows the subject does not own |
| `RECOMPUTE` | `REFRESH_MATVIEW`, `RECOMPUTE_TABLE` | shared aggregates rebuilt, never row-deleted |
| `RETAIN` | `ANONYMIZE_ROWS`, `RETAIN_ROWS` (new) | legally retained. PII scrubbed (`ANONYMIZE`) or kept untouched under legal hold (`RETAIN`). **Never deleted** |
| `SCHEDULE` | `SCHEDULE_EXTERNAL` (new) | registered external artifact with a handler. A task is recorded, and it cannot be verified in the DB |
| `MANUAL_REVIEW` | `MANUAL_REVIEW` | shared records needing a human decision |
| `UNRESOLVED` | `UNRESOLVED` (new) | opaque or uninspectable dependency (`OPAQUE` SQL, external artifact without a handler) |

`lae.contracts.enums.action_category(PlanAction) -> ActionCategory` is the single mapping.

## 2. Artifacts (brief §6D)
- `ArtifactKind` adds `CACHE` (a key-addressable cache table, invalidated by key) and `EXTERNAL` (outside PostgreSQL, **not inspectable**).
- `RefreshStrategy` adds `INVALIDATE_BY_KEY` (CACHE) and `EXTERNAL_TASK` (EXTERNAL with a handler).
- `derived_artifact` adds `is_inspectable boolean NOT NULL DEFAULT true` (false for EXTERNAL).
- `ConflictCode` adds `EXTERNAL_ARTIFACT` (WARNING).
- An `OPAQUE` / uninspectable artifact gives an `UNRESOLVED` plan item plus an `OPAQUE_ARTIFACT` BLOCKING conflict. The conflict can be **acknowledged** (that resolves it), so execution may proceed, but the outcome can be at most `PARTIAL`.

## 3. Verification outcome and coverage (brief §10)
- New `VerificationOutcome`: `VERIFIED | PARTIAL | FAILED`. `verification_run` adds `outcome text`, `required_artifacts int`, `verified_artifacts int`.
- A **required artifact** is a plan item whose category is DELETE, INVALIDATE, RECOMPUTE, RETAIN, SCHEDULE, MANUAL_REVIEW or UNRESOLVED (every item).
- **Verified artifact:** every check bound to that item passed. SCHEDULE / MANUAL_REVIEW / UNRESOLVED items are *never* verified in-DB.
- `coverage = verified_artifacts / required_artifacts` (1.0 when required = 0).
- `FAILED` if any mandatory check fails (including `RESIDUAL_VALUE_SCAN` and `REDISCOVERY_EMPTY`). `VERIFIED` only if nothing failed **and** coverage = 1.0. Otherwise `PARTIAL`.
- `VerificationStatus` (RUNNING/PASSED/FAILED/ERROR) stays as the run status. PASSED ⇔ no check failed.

## 4. API (brief §12)
- Prefix changes from `/api/v1` to **`/api`**. The brief's endpoints are implemented exactly:
  `POST/GET /api/erasure-requests`, `GET /api/erasure-requests/{id}`, `POST /api/discovery/{request_id}`, `GET /api/lineage/{request_id}`,
  `POST /api/erasure-plans/{request_id}/generate`, `GET /api/erasure-plans/{plan_id}`, `POST /api/erasure-plans/{plan_id}/approve`,
  `POST /api/erasure-plans/{plan_id}/execute` (body `mode`), `GET /api/verification/{request_id}`, `GET /api/audit/{request_id}`, `GET /api/health`.
- Supporting endpoints (needed by the UI, no fabricated data): `GET /api/stats`, `GET /api/customers?q=` (synthetic customers, **masked** email), `GET /api/lineage/schema` (table/column graph of the latest snapshot), `POST /api/erasure-plans/{plan_id}/conflicts/{conflict_id}/acknowledge`, `POST /api/verification/{request_id}/run`, `GET /api/audit/events`, `POST /api/audit/verify-chain`, `GET /api/evidence/{request_id}/download`, `GET /api/evidence/{request_id}/leaves`, `GET /api/benchmarks`.
- **Local auth:** mutating endpoints require header `X-LAE-Token` equal to `LAE_API_TOKEN` (constant-time compare). Missing or wrong token gives 401 `UNAUTHORIZED`. The actor is taken from `X-LAE-Actor` (default `local-operator`).
- Executing a LIVE plan runs verification and issues evidence in the same API call (each step in its own transaction, as before).

## 5. Dataset (brief §5)
- `app.document` (FK → customer, CASCADE, `body text` that contains the customer's name/email) and `app.customer_profile_cache` (no FK, `customer_id` + `payload jsonb`, registered `CACHE` artifact) are added to the demo schema.
- `document_embedding` sources from `app.document` (registered mapping `source_document_id → app.document.id`).
- Default scale: 1 000 customers, 3 000 orders, 1 000 tickets, 500 documents, 500 embeddings. All configurable.

## 6. Frontend
Tailwind CSS is added to the approved React + Vite + TS stack.
