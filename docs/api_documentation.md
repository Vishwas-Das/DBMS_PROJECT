# API Documentation

This document describes the FastAPI application in `backend/src/lae/api/app.py` (factory `create_app`, ASGI entry point `lae.api.main:app`). The schemas live in `backend/src/lae/api/schemas.py`, and the exported OpenAPI document is `docs/openapi.json` (23 paths, 25 operations). The description matches the code at git commit `57de8e8`, which includes the security-review changes (every route except health requires the token, four-eyes execution, no snapshot creation on the GET discovery path, duplicate-request check). Commit `118846f` after it changed only `docs/security_analysis.md`.

All samples below are **real** requests and responses, recorded against a running server:

- `uvicorn lae.api.main:app --port 8001`, started from a temporary directory without `.env`.
- Database `lae_test_lead`, migrated to head (including `0002_security_hardening`) and re-seeded with `python -m db.fixtures.generator --scale tiny --seed 7` right before sampling. `LAE_DATABASE_URL` pointed at that database as `lae_owner`, not as the non-superuser `lae_app` role that `.env.example` recommends.
- A fresh random `LAE_HMAC_KEY`, a fresh Ed25519 key from `lae keys generate`, and `LAE_TARGET_SCHEMAS=app`.

Responses are **trimmed** for length (`"... n more trimmed ..."` marks removed list elements), and the API token is replaced by `<token>`. The database already held events and requests from earlier integration-test runs, which is why sequence numbers do not start at 1 and some counts include older rows. HMAC values in the samples are keyed digests, not personal data. They are meaningless without the (discarded) key.

---

## 1. Conventions

- **Base path** `/api`. JSON uses snake_case. UUIDs are strings, hashes are 64 lowercase hex characters, timestamps are ISO-8601 UTC.
- **Pydantic v2** with `extra="forbid"` on every request and response model. Unknown fields give 422.
- **Authentication (local prototype):** every route except `GET /api/health` requires the header `X-LAE-Token` equal to `LAE_API_TOKEN` (≥ 24 characters, compared in constant time over UTF-8 bytes). This includes GET routes and `POST /api/audit/verify-chain`. A missing or wrong token gives 401 `UNAUTHORIZED`. If the server has no token configured, every protected route returns 401. A `TrustedHostMiddleware` only accepts the `Host` values `localhost` and `127.0.0.1` (mitigates DNS rebinding).
- **Actor:** `X-LAE-Actor` (default `local-operator`, trimmed, max 64 characters). It is recorded in audit events and as `approved_by`. It is an **unverified attestation**, not an identity (risks §2.6).
- **Errors** always use the envelope `{"error": {"code", "message", "details"}}`. The HTTP status comes from `STATUS_BY_CODE` in `app.py`:

| `code` | HTTP | Raised by (examples) |
|---|---|---|
| `NOT_FOUND`, `SUBJECT_NOT_FOUND` | 404 | unknown id, unknown customer, no evidence yet |
| `INVALID_INPUT`, `UNKNOWN_TABLE`, `UNKNOWN_COLUMN`, `CANONICALIZATION` | 422 | request validation (field locations only, never the submitted values), domain validation |
| `INVALID_STATE`, `PLAN_BLOCKED`, `PLAN_HASH_MISMATCH`, `PLAN`, `STALE_PLAN`, `ALREADY_EXECUTED`, `ROW_COUNT_MISMATCH` | 409 | state machine, approval binding, drift, rowcount assertions |
| `DISCOVERY_LIMIT` | 413 | `max_depth` / `max_records` exceeded |
| `SERIALIZATION_CONFLICT` | 503 | PostgreSQL serialization failure (not retried automatically) |
| `UNAUTHORIZED` | 401 | missing or wrong token |
| **any other code** | by family | `NotFoundError` 404, `ValidationError` 422, `PlanError` / `ExecutionError` 409 (for example `CONFLICT_NOT_ACKNOWLEDGEABLE`, `INVALID_PLAN`, `STEP_FAILED`). Everything else is 500 (`VERIFICATION`, `AUDIT_INTEGRITY`, `CONFIGURATION`, `GRAPH_INTEGRITY`, `CATALOG`, `INTERNAL`) |


- **Logging:** the access-log middleware records method, path template, status and duration, plus an `X-Request-Id` response header. Bodies and query strings are never logged.
- **Long operations are synchronous.** LIVE execution also runs verification and evidence issuance in the same HTTP call.

### Typical workflow

```
POST /erasure-requests → POST /discovery/{rid} (optional) → POST /erasure-plans/{rid}/generate
→ [POST /erasure-plans/{pid}/conflicts/{cid}/acknowledge]* → POST /erasure-plans/{pid}/execute {DRY_RUN}
→ POST /erasure-plans/{pid}/approve {plan_hash} → POST /erasure-plans/{pid}/execute {LIVE}
→ GET /verification/{rid} → GET /audit/{rid} → GET /evidence/{rid}/download + /leaves → lae verify-evidence
```

---

## 2. Health, statistics, lookup

### 2.1 `GET /api/health`
- **Auth:** none. **Request:** none. **Response:** `HealthOut {status: "ok"|"degraded", database, pgvector, pgcrypto, audit_chain_valid, signing_key_id}`.
- Returns 200 with `status:"degraded", database:false` if the database is unreachable. `audit_chain_valid` comes from `verify_chain` over the whole chain. `status` is `"degraded"` if the chain is invalid.

```bash
curl -s -X GET "http://127.0.0.1:8001/api/health" 
```

HTTP 200

```json
{
  "status": "ok",
  "database": true,
  "pgvector": "0.8.7",
  "pgcrypto": "1.3",
  "audit_chain_valid": true,
  "signing_key_id": "c5ef0ed6ca133d1a"
}
```


### 2.2 `GET /api/stats`
- **Auth:** token. **Response:** `StatsOut`: live `COUNT`s (synthetic customers, requests by status, tables and edges of the latest snapshot, registered artifacts, audit events, latest snapshot id and time).

```bash
curl -s -X GET "http://127.0.0.1:8001/api/stats" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver"
```

HTTP 200

```json
{
  "synthetic_customers": 20,
  "erasure_requests": 12,
  "verified_requests": 1,
  "partial_requests": 0,
  "pending_requests": 0,
  "failed_requests": 0,
  "discovered_tables": 17,
  "discovered_edges": 45,
  "registered_artifacts": 6,
  "audit_events": 62,
  "latest_snapshot_id": "3aa0c5bf-5983-497d-a1d1-53f18b1532c3",
  "latest_snapshot_at": "2026-11-04T17:47:34.349443Z"
}
```

**A GET without the token**

```bash
curl -s -X GET "http://127.0.0.1:8001/api/stats" 
```

HTTP 401

```json
{
  "error": {
    "code": "UNAUTHORIZED",
    "message": "missing or invalid X-LAE-Token",
    "details": {}
  }
}
```


### 2.3 `GET /api/customers?q=&limit=`
- **Auth:** token. **Query:** `q` (≤ 64 characters, matches `full_name ILIKE` or an exact id), `limit` 1–100 (default 25).
- **Response:** `list[CustomerOut {id, display_name, email_masked, open_request_id}]`. The e-mail is masked as first character + `***@domain` (`services/impl.py: _mask_email`). `id` is the synthetic PK, kept by Lead decision (CR frontend-engineer-1 §7).

```bash
curl -s -X GET "http://127.0.0.1:8001/api/customers?q=18&limit=3" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver"
```

HTTP 200

```json
[
  {
    "id": 18,
    "display_name": "Ulla Stillwater",
    "email_masked": "u***@example.test",
    "open_request_id": null
  }
]
```


### 2.4 `GET /api/benchmarks?scenario=`
- **Auth:** token. **Response:** `list[BenchmarkRunOut]` from `lae.benchmark_run`. It is empty here, because no benchmark runs are stored.

```bash
curl -s -X GET "http://127.0.0.1:8001/api/benchmarks" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver"
```

HTTP 200

```json
[]
```


---

## 3. Erasure requests

### 3.1 `POST /api/erasure-requests`
- **Auth:** token. **Request:** `ErasureRequestCreate {subject_root = "customer", subject_key: list[int|str] (1–8 items), reason?: str ≤ 500}`. **Response:** 201 `ErasureRequestOut {id, subject_root, subject_key_hmac, status, requested_by, created_at, completed_at, current_plan_id, latest_outcome}`.
- Reuses the latest snapshot if the live catalog fingerprint matches it, otherwise creates one (`pipeline.latest_or_new_snapshot`). Checks that the subject row exists. Appends `REQUEST_RECEIVED` with the subject key **HMAC** only. The raw key is stored transiently in `lae.erasure_request.subject_key` until purge.
- **Errors:** 401, 404 `SUBJECT_NOT_FOUND` / `NOT_FOUND` (unknown root), 422 (validation, arity), 409 `INVALID_STATE` if an open request (status not VERIFIED or CANCELLED) already exists for the same subject. The key is canonicalised from the DB row first, so `'18'` and `18` count as the same subject.

```bash
curl -s -X POST "http://127.0.0.1:8001/api/erasure-requests" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver" -H "Content-Type: application/json" -d '{"subject_root": "customer", "subject_key": [18], "reason": "docs sample"}'
```

HTTP 201

```json
{
  "id": "adca6a98-f40c-44c5-b9af-5dfd8f06a8df",
  "subject_root": "customer",
  "subject_key_hmac": "2aaa7fe4059402b96c44009e7dfaab37c35e9902c7a73fe3fc444ac3ccba1b60",
  "status": "RECEIVED",
  "requested_by": "docs-approver",
  "created_at": "2026-11-04T17:50:33.425207Z",
  "completed_at": null,
  "current_plan_id": null,
  "latest_outcome": null
}
```

**Duplicate open request**

```bash
curl -s -X POST "http://127.0.0.1:8001/api/erasure-requests" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver" -H "Content-Type: application/json" -d '{"subject_key": [18]}'
```

HTTP 409

```json
{
  "error": {
    "code": "INVALID_STATE",
    "message": "an open erasure request already exists for this subject",
    "details": {
      "request_id": "adca6a98-f40c-44c5-b9af-5dfd8f06a8df"
    }
  }
}
```

**Missing token**

```bash
curl -s -X POST "http://127.0.0.1:8001/api/erasure-requests"  -H "Content-Type: application/json" -d '{"subject_key": [18]}'
```

HTTP 401

```json
{
  "error": {
    "code": "UNAUTHORIZED",
    "message": "missing or invalid X-LAE-Token",
    "details": {}
  }
}
```

**Validation error (values are not echoed)**

```bash
curl -s -X POST "http://127.0.0.1:8001/api/erasure-requests" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver" -H "Content-Type: application/json" -d '{"subject_key": []}'
```

HTTP 422

```json
{
  "error": {
    "code": "INVALID_INPUT",
    "message": "request validation failed",
    "details": {
      "errors": [
        {
          "loc": [
            "body",
            "subject_key"
          ],
          "msg": "List should have at least 1 item after validation, not 0"
        }
      ]
    }
  }
}
```

**Unknown subject**

```bash
curl -s -X POST "http://127.0.0.1:8001/api/erasure-requests" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver" -H "Content-Type: application/json" -d '{"subject_key": [999999]}'
```

HTTP 404

```json
{
  "error": {
    "code": "SUBJECT_NOT_FOUND",
    "message": "no such data subject",
    "details": {}
  }
}
```


### 3.2 `GET /api/erasure-requests?limit=`
- **Auth:** token. **Query:** `limit` 1–500 (default 100). **Response:** `list[ErasureRequestOut]`, newest first.

```bash
curl -s -X GET "http://127.0.0.1:8001/api/erasure-requests?limit=2" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver"
```

HTTP 200

```json
[
  {
    "id": "adca6a98-f40c-44c5-b9af-5dfd8f06a8df",
    "subject_root": "customer",
    "subject_key_hmac": "2aaa7fe4059402b96c44009e7dfaab37c35e9902c7a73fe3fc444ac3ccba1b60",
    "status": "RECEIVED",
    "requested_by": "docs-approver",
    "created_at": "2026-11-04T17:50:33.425207Z",
    "completed_at": null,
    "current_plan_id": null,
    "latest_outcome": null
  },
  {
    "id": "80ed414d-7824-4bc8-bb47-183782135f37",
    "subject_root": "customer",
    "subject_key_hmac": "a7bab56cf46c303e4d021a2368880a26b693467e1e5f2c803775815093a66bce",
    "status": "CANCELLED",
    "requested_by": "operator",
    "created_at": "2026-11-04T17:48:05.438816Z",
    "completed_at": null,
    "current_plan_id": null,
    "latest_outcome": null
  }
]
```


### 3.3 `GET /api/erasure-requests/{request_id}`
- **Auth:** token. **Response:** `ErasureRequestOut`. **Errors:** 404.

```bash
curl -s -X GET "http://127.0.0.1:8001/api/erasure-requests/adca6a98-f40c-44c5-b9af-5dfd8f06a8df" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver"
```

HTTP 200

```json
{
  "id": "adca6a98-f40c-44c5-b9af-5dfd8f06a8df",
  "subject_root": "customer",
  "subject_key_hmac": "2aaa7fe4059402b96c44009e7dfaab37c35e9902c7a73fe3fc444ac3ccba1b60",
  "status": "RECEIVED",
  "requested_by": "docs-approver",
  "created_at": "2026-11-04T17:50:33.425207Z",
  "completed_at": null,
  "current_plan_id": null,
  "latest_outcome": null
}
```

**Unknown id**

```bash
curl -s -X GET "http://127.0.0.1:8001/api/erasure-requests/00000000-0000-0000-0000-000000000001" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver"
```

HTTP 404

```json
{
  "error": {
    "code": "NOT_FOUND",
    "message": "erasure_request not found",
    "details": {
      "entity": "erasure_request",
      "id": "00000000-0000-0000-0000-000000000001"
    }
  }
}
```


---

## 4. Discovery and lineage

### 4.1 `POST /api/discovery/{request_id}`
- **Auth:** token. **Request:** none. **Response:** `DiscoveryOut {request_id, snapshot_id, schema_fingerprint, tables, table_edges, column_edges, records_found, records_by_role, affected_artifacts, external_dependencies, record_set_hash, duration_ms, warnings}`.
- Runs record discovery in a `REPEATABLE READ` read-only transaction and appends `DISCOVERY_COMPLETED`. **The result is not stored.** Plan generation re-runs discovery itself.
- **Errors:** 404, 409 `INVALID_STATE` (subject key already purged), 413.

```bash
curl -s -X POST "http://127.0.0.1:8001/api/discovery/adca6a98-f40c-44c5-b9af-5dfd8f06a8df" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver"
```

HTTP 200

```json
{
  "request_id": "adca6a98-f40c-44c5-b9af-5dfd8f06a8df",
  "snapshot_id": "3aa0c5bf-5983-497d-a1d1-53f18b1532c3",
  "schema_fingerprint": "59ec6eeab9a2d0cb88d8accc9599bc76955c4e56af4d46a4f5d5b5bd893d4228",
  "tables": 17,
  "table_edges": 20,
  "column_edges": 27,
  "records_found": 10,
  "records_by_role": {
    "SUBJECT_ROOT": 1,
    "OWNED": 9
  },
  "affected_artifacts": {
    "app.customer_profile_cache": "KEY_PRESERVING"
  },
  "external_dependencies": [
    "external.crm_export_files"
  ],
  "record_set_hash": "4ae209a8cb1ff2dff65c5c9158514795c3e0f8b767a78cf7b82970ca435b3e36",
  "duration_ms": 618,
  "warnings": [
    "2 unconfirmed table edge(s) from subject-reachable tables were not traversed"
  ]
}
```


### 4.2 `GET /api/discovery/{request_id}`
- **Auth:** token. **Response:** `DiscoveryOut`.
- **Implementation note:** this does **not** return a stored result. `LaeServices.latest_discovery` re-runs discovery read-only, without an audit event. It uses the latest existing snapshot and never creates one (404 if no snapshot exists). The CR frontend-engineer-1 decision text says "latest result, no re-run", which differs from the code.

```bash
curl -s -X GET "http://127.0.0.1:8001/api/discovery/adca6a98-f40c-44c5-b9af-5dfd8f06a8df" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver"
```

HTTP 200

```json
{
  "request_id": "adca6a98-f40c-44c5-b9af-5dfd8f06a8df",
  "snapshot_id": "3aa0c5bf-5983-497d-a1d1-53f18b1532c3",
  "schema_fingerprint": "59ec6eeab9a2d0cb88d8accc9599bc76955c4e56af4d46a4f5d5b5bd893d4228",
  "tables": 17,
  "table_edges": 20,
  "column_edges": 27,
  "records_found": 10,
  "records_by_role": {
    "SUBJECT_ROOT": 1,
    "OWNED": 9
  },
  "affected_artifacts": {
    "app.customer_profile_cache": "KEY_PRESERVING"
  },
  "external_dependencies": [
    "external.crm_export_files"
  ],
  "record_set_hash": "4ae209a8cb1ff2dff65c5c9158514795c3e0f8b767a78cf7b82970ca435b3e36",
  "duration_ms": 210,
  "warnings": [
    "2 unconfirmed table edge(s) from subject-reachable tables were not traversed"
  ]
}
```


### 4.3 `GET /api/lineage/schema?level=TABLE|COLUMN`
- **Auth:** token. **Response:** `SchemaGraphOut {snapshot_id, level, nodes: list[GraphNodeOut], edges: list[GraphEdgeOut]}` for the latest snapshot. Edges of **all** statuses are included (SUGGESTED as well), so the UI can offer confirmation. `on_delete_action` is the raw `confdeltype` letter (`a` no action, `r` restrict, `c` cascade, `n` set null, `d` set default). `evidence` merges the edge `details` with origin, constraint and artifact id. It never creates a snapshot: 404 before the first discovery.

```bash
curl -s -X GET "http://127.0.0.1:8001/api/lineage/schema?level=TABLE" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver"
```

HTTP 200

```json
{
  "snapshot_id": "3aa0c5bf-5983-497d-a1d1-53f18b1532c3",
  "level": "TABLE",
  "nodes": [
    {
      "id": "t:app.customer",
      "kind": "table",
      "schema_name": "app",
      "table_name": "customer",
      "column_name": null,
      "table_kind": "TABLE",
      "pii_category": null,
      "is_artifact": false
    },
    {
      "id": "t:app.customer_address",
      "kind": "table",
      "schema_name": "app",
      "table_name": "customer_address",
      "column_name": null,
      "table_kind": "TABLE",
      "pii_category": null,
      "is_artifact": false
    }
  ],
  "edges": [
    {
      "id": "0a692223-40e4-5bb5-b141-083211b87fae",
      "source": "t:app.customer",
      "target": "t:app.loyalty_account",
      "level": "TABLE",
      "origin": "DECLARED_FK",
      "transform": "FOREIGN_KEY",
      "traceability": "KEY_PRESERVING",
      "status": "CONFIRMED",
      "source_columns": [
        "id"
      ],
      "target_columns": [
        "customer_id"
      ],
      "on_delete_action": "a",
      "confidence": 1.0,
      "evidence": {
        "artifact_id": null,
        "constraint": "loyalty_account_customer_id_fkey",
        "constraint_name": "loyalty_account_customer_id_fkey",
        "deferrable": false,
        "evidence": "pg_constraint",
        "on_delete": "a",
        "on_update": "a",
        "origin": "DECLARED_FK"
      }
    },
    {
      "id": "3b7d1bce-1a44-5816-9987-f455b5ec7a52",
      "source": "t:app.customer_ltv",
      "target": "t:app.customer_segment",
      "level": "TABLE",
      "origin": "SQL_LINEAGE",
      "transform": "IDENTITY",
      "traceability": "KEY_PRESERVING",
      "status": "CONFIRMED",
      "source_columns": [
        "customer_id"
      ],
      "target_columns": [
        "customer_id"
      ],
      "on_delete_action": null,
      "confidence": 1.0,
      "evidence": {
        "artifact_id": null,
        "constraint_name": null,
        "evidence": "sqlglot",
        "key_mapping": "inferred",
        "origin": "SQL_LINEAGE",
        "sqlglot_version": "30.29.0"
      }
    },
    "... 18 more edges trimmed ..."
  ]
}
```


### 4.4 `GET /api/lineage/{request_id}?level=TABLE|COLUMN`
- **Auth:** token. **Response:** `LineageOut {request_id, plan_id, snapshot_id, schema_graph, records: list[RecordNodeOut], record_edges: list[RecordEdgeOut]}`. Record ids are **key HMACs**, never raw keys.

```bash
curl -s -X GET "http://127.0.0.1:8001/api/lineage/adca6a98-f40c-44c5-b9af-5dfd8f06a8df" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver"
```

HTTP 200

```json
{
  "request_id": "adca6a98-f40c-44c5-b9af-5dfd8f06a8df",
  "plan_id": "c313dceb-2f83-413a-83d3-7c6ac8e52c8e",
  "snapshot_id": "3aa0c5bf-5983-497d-a1d1-53f18b1532c3",
  "schema_graph": {
    "snapshot_id": "3aa0c5bf-5983-497d-a1d1-53f18b1532c3",
    "level": "TABLE",
    "nodes": [
      "... 17 nodes trimmed (same shape as /api/lineage/schema) ..."
    ],
    "edges": [
      "... 20 edges trimmed ..."
    ]
  },
  "records": [
    {
      "id": "2aaa7fe4059402b96c44009e7dfaab37c35e9902c7a73fe3fc444ac3ccba1b60",
      "table": "app.customer",
      "role": "SUBJECT_ROOT",
      "depth": 0,
      "is_predicate": false
    },
    {
      "id": "6ae17a4a74fdc9a567ab5c7d55a496f2a7be7ae8e924d416d6937125f9409119",
      "table": "app.customer_address",
      "role": "OWNED",
      "depth": 1,
      "is_predicate": false
    },
    "... 8 more trimmed ..."
  ],
  "record_edges": [
    {
      "source": "2aaa7fe4059402b96c44009e7dfaab37c35e9902c7a73fe3fc444ac3ccba1b60",
      "target": "6ae17a4a74fdc9a567ab5c7d55a496f2a7be7ae8e924d416d6937125f9409119",
      "edge_id": "1ce9c9f5-c6a2-5002-8ea9-8ae200f865c6"
    },
    {
      "source": "2aaa7fe4059402b96c44009e7dfaab37c35e9902c7a73fe3fc444ac3ccba1b60",
      "target": "f9766340e3253a8fcec2692917b898c15d949c1e001d633b79fd004fa011acf0",
      "edge_id": "1ce9c9f5-c6a2-5002-8ea9-8ae200f865c6"
    },
    "... 7 more trimmed ..."
  ]
}
```


### 4.5 `POST /api/lineage-edges/{edge_id}/status`
- **Auth:** token. **Request:** `EdgeStatusUpdate {status: "CONFIRMED"|"REJECTED"}`. **Response:** `GraphEdgeOut`.
- Only `INFERRED` edges may change (otherwise 422 `INVALID_INPUT`). Appends `EDGE_STATUS_CHANGED`. A confirmed edge is traversed by the next discovery.
- The sample confirms a **residual-scan suggestion** (`evidence: residual_scan`, confidence 0.5) for `customer.email → marketing_event_log.email`. Its `hit_count` is the count from the verification run that first created the edge.

```bash
curl -s -X POST "http://127.0.0.1:8001/api/lineage-edges/4b540ac3-c92e-5407-8abe-d2936d368a5f/status" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver" -H "Content-Type: application/json" -d '{"status": "CONFIRMED"}'
```

HTTP 200

```json
{
  "id": "4b540ac3-c92e-5407-8abe-d2936d368a5f",
  "source": "t:app.customer",
  "target": "t:app.marketing_event_log",
  "level": "TABLE",
  "origin": "INFERRED",
  "transform": "IDENTITY",
  "traceability": "KEY_PRESERVING",
  "status": "CONFIRMED",
  "source_columns": [
    "email"
  ],
  "target_columns": [
    "email"
  ],
  "on_delete_action": null,
  "confidence": 0.5,
  "evidence": {
    "artifact_id": null,
    "constraint_name": null,
    "evidence": "residual_scan",
    "hit_count": 10,
    "note": "value match found by the lineage-independent scan",
    "origin": "INFERRED"
  }
}
```


---

## 5. Plans

### 5.1 `POST /api/erasure-plans/{request_id}/generate`
- **Auth:** token. **Request (optional):** `PlanGenerate {max_depth = 10 (1–50), max_records = 100000 (1–10^7)}`. **Response:** 201 `PlanOut {id, request_id, version, status, snapshot_id, schema_fingerprint, record_set_hash, plan_hash, record_count, items: list[PlanItemOut], conflicts: list[PlanConflictOut], counts_by_category, is_blocked, created_at, approved_by, approved_at}`.
- `PlanItemOut {id, step_order, action, category, schema_name, table_name, lineage_level, key_columns, record_count, record_key_hmacs, target_columns, expected_row_count, depends_on_steps, reason_edge_ids, reason, retention_policy}`. Raw record keys are **never** returned, only HMACs.
- `PlanConflictOut {id, code, severity, schema_name, table_name, message, resolution, acknowledgeable}`.
- A previous live plan of the request becomes `SUPERSEDED`. The new plan is `BLOCKED` if any BLOCKING conflict is unresolved, else `DRAFT`. Residual probes (HMACs) are stored. Appends `PLAN_CREATED`.
- **Errors:** 404, 409, 413.

**Subject 18 (no shared or hidden data): DRAFT plan**

```bash
curl -s -X POST "http://127.0.0.1:8001/api/erasure-plans/adca6a98-f40c-44c5-b9af-5dfd8f06a8df/generate" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver" -H "Content-Type: application/json" -d '{"max_depth": 10}'
```

HTTP 201

```json
{
  "id": "c313dceb-2f83-413a-83d3-7c6ac8e52c8e",
  "request_id": "adca6a98-f40c-44c5-b9af-5dfd8f06a8df",
  "version": 1,
  "status": "DRAFT",
  "snapshot_id": "3aa0c5bf-5983-497d-a1d1-53f18b1532c3",
  "schema_fingerprint": "59ec6eeab9a2d0cb88d8accc9599bc76955c4e56af4d46a4f5d5b5bd893d4228",
  "record_set_hash": "4ae209a8cb1ff2dff65c5c9158514795c3e0f8b767a78cf7b82970ca435b3e36",
  "plan_hash": "51ead16db0e340daea7ccc87be359cc3b0e1f9c45e9b6fdaffb6690bef405a20",
  "record_count": 10,
  "items": [
    {
      "id": "50349190-80e4-4c66-a7eb-e42259f097bf",
      "step_order": 1,
      "action": "ANONYMIZE_ROWS",
      "category": "RETAIN",
      "schema_name": "app",
      "table_name": "purchase_order",
      "lineage_level": "RECORD",
      "key_columns": [
        "id"
      ],
      "record_count": 1,
      "record_key_hmacs": [
        "7f3e49a48d9e9549a0cb4bec28afcfc0cedbec955d36a11150834dcb742e080d"
      ],
      "target_columns": [
        "customer_id",
        "shipping_address",
        "shipping_name"
      ],
      "expected_row_count": 1,
      "depends_on_steps": [],
      "reason_edge_ids": [
        "5bd59208-f687-5d09-8888-3f22a4463886"
      ],
      "reason": "ANONYMIZE 1 row of app.purchase_order: retention policy ANONYMIZE; columns customer_id (NULLIFY: references rows being deleted), shipping_address (NULLIFY), shipping_name (PSEUDONYMIZE)",
      "retention_policy": "ANONYMIZE (tax record retention (synthetic))"
    },
    {
      "id": "0b1d19bd-bdaa-455d-9de4-e6fa30234025",
      "step_order": 2,
      "action": "DELETE_ROWS",
      "category": "DELETE",
      "schema_name": "app",
      "table_name": "loyalty_account",
      "lineage_level": "RECORD",
      "key_columns": [
        "customer_id"
      ],
      "record_count": 1,
      "record_key_hmacs": [
        "706659eb11f83a9248aad8fd0d6843774a56152b4b2b09e817f3c8d4e3f16522"
      ],
      "target_columns": [],
      "expected_row_count": 1,
      "depends_on_steps": [],
      "reason_edge_ids": [
        "0a692223-40e4-5bb5-b141-083211b87fae"
      ],
      "reason": "DELETE 1 row of app.loyalty_account: owned by subject via FK loyalty_account_customer_id_fkey (ON DELETE NO ACTION)",
      "retention_policy": null
    },
    {
      "id": "76c830e0-f248-4eb2-a6c6-83a813eb6e32",
      "step_order": 3,
      "action": "DELETE_ROWS",
      "category": "DELETE",
      "schema_name": "app",
      "table_name": "customer",
      "lineage_level": "RECORD",
      "key_columns": [
        "id"
      ],
      "record_count": 1,
      "record_key_hmacs": [
        "2aaa7fe4059402b96c44009e7dfaab37c35e9902c7a73fe3fc444ac3ccba1b60"
      ],
      "target_columns": [],
      "expected_row_count": 1,
      "depends_on_steps": [
        1,
        2
      ],
      "reason_edge_ids": [],
      "reason": "DELETE 1 row of app.customer: subject root; cascade_check customer_address_customer_id_fkey: app.customer_address(customer_id) <- (id) = 2; cascade_check document_customer_id_fkey: app.document(customer_id) <- (id) = 0",
      "retention_policy": null
    },
    "... 2 more trimmed ..."
  ],
  "conflicts": [
    {
      "id": "7e48f0f2-ac21-4c7b-921f-d312c9a062b2",
      "code": "RETAINED_WITH_PII",
      "severity": "WARNING",
      "schema_name": "app",
      "table_name": "purchase_order",
      "message": "app.purchase_order: column(s) shipping_name are pseudonymized, not anonymized; retained rows may still be re-identifiable",
      "resolution": null,
      "acknowledgeable": true
    },
    {
      "id": "8a7ab2c1-813f-47a2-8f99-57b19229b8e5",
      "code": "EXTERNAL_ARTIFACT",
      "severity": "WARNING",
      "schema_name": "external",
      "table_name": "crm_export_files",
      "message": "external.crm_export_files: external artifact crm_export_files is erased by a scheduled task and cannot be verified in the database",
      "resolution": null,
      "acknowledgeable": true
    }
  ],
  "counts_by_category": {
    "RETAIN": 2,
    "DELETE": 3,
    "INVALIDATE": 1,
    "RECOMPUTE": 3,
    "SCHEDULE": 1
  },
  "is_blocked": false,
  "created_at": "2026-11-04T17:50:35.255504Z",
  "approved_by": null,
  "approved_at": null
}
```

**Subject 7 (shared messages, referrals, hidden copies): BLOCKED plan**

```bash
curl -s -X POST "http://127.0.0.1:8001/api/erasure-plans/619695b4-81ba-4ad0-9fb5-a54d7bee2c9e/generate" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver"
```

HTTP 201

```json
{
  "id": "42a51f85-d3a9-47ab-bb8a-da1049cc507f",
  "request_id": "619695b4-81ba-4ad0-9fb5-a54d7bee2c9e",
  "version": 1,
  "status": "BLOCKED",
  "snapshot_id": "3aa0c5bf-5983-497d-a1d1-53f18b1532c3",
  "schema_fingerprint": "59ec6eeab9a2d0cb88d8accc9599bc76955c4e56af4d46a4f5d5b5bd893d4228",
  "record_set_hash": "4bd8433ea2f4f38f7ae2d444c46600d9a3ae35aafcf92ac9c6d5d213ab455a54",
  "plan_hash": "3925a26027207740d03e3958598a64f8bdd67bac54899959321c6085ae7aafa6",
  "record_count": 44,
  "items": [
    {
      "id": "ef40c6be-1588-45e6-8c7c-40b7c7db734c",
      "step_order": 1,
      "action": "NULLIFY_REFERENCE",
      "category": "INVALIDATE",
      "schema_name": "app",
      "table_name": "customer",
      "lineage_level": "RECORD",
      "key_columns": [
        "id"
      ],
      "record_count": 2,
      "record_key_hmacs": [
        "5e8eb90d2f65f7f69ee98328e39cec0596498d5f3792dd6f5d08c020461fc182",
        "47cc419d31208e307b6a466234643e9c49903d431c27731f8f13b2172df80223"
      ],
      "target_columns": [
        "referrer_id"
      ],
      "expected_row_count": 2,
      "depends_on_steps": [],
      "reason_edge_ids": [
        "f50bf56b-ec97-570a-b2b5-555b3f0bf97f"
      ],
      "reason": "NULLIFY_REFERENCE referrer_id on 2 rows of app.customer: rows belong to other subjects and reference the subject via FK customer_referrer_id_fkey (ON DELETE SET NULL)",
      "retention_policy": null
    },
    {
      "id": "c5d3cb2d-b8dc-4495-9234-7c7cc838c4a4",
      "step_order": 2,
      "action": "ANONYMIZE_ROWS",
      "category": "RETAIN",
      "schema_name": "app",
      "table_name": "purchase_order",
      "lineage_level": "RECORD",
      "key_columns": [
        "id"
      ],
      "record_count": 3,
      "record_key_hmacs": [
        "723daa47d7d61a213a687e15a2454a8fb61fe40cc9f6c15b052a0f574f1e50c1",
        "6ffe6c5dcdc861cbcb9e375ba9b1d433b70ca0344a4a1f8d78fbb95d92793301",
        "5dd7767b61707a6d189e1b40c772c0bf3b9d369786714d7840b419901c1867f4"
      ],
      "target_columns": [
        "customer_id",
        "shipping_address",
        "shipping_name"
      ],
      "expected_row_count": 3,
      "depends_on_steps": [],
      "reason_edge_ids": [
        "5bd59208-f687-5d09-8888-3f22a4463886"
      ],
      "reason": "ANONYMIZE 3 rows of app.purchase_order: retention policy ANONYMIZE; columns customer_id (NULLIFY: references rows being deleted), shipping_address (NULLIFY), shipping_name (PSEUDONYMIZE)",
      "retention_policy": "ANONYMIZE (tax record retention (synthetic))"
    },
    {
      "id": "9559a745-1589-46e0-b4d1-90f23a1c0dd9",
      "step_order": 3,
      "action": "NULLIFY_REFERENCE",
      "category": "INVALIDATE",
      "schema_name": "app",
      "table_name": "ticket_message",
      "lineage_level": "RECORD",
      "key_columns": [
        "id"
      ],
      "record_count": 1,
      "record_key_hmacs": [
        "a24a915ea73314c6d6801c3da7943564e51e27beb67c2f670b4e0b62611ea06c"
      ],
      "target_columns": [
        "author_customer_id"
      ],
      "expected_row_count": 1,
      "depends_on_steps": [],
      "reason_edge_ids": [
        "863d6009-86aa-5c91-9a9e-8a2907ba715e"
      ],
      "reason": "NULLIFY_REFERENCE author_customer_id on 1 row of app.ticket_message: shared rows reference rows being deleted via FK ticket_message_author_customer_id_fkey (ON DELETE NO ACTION)",
      "retention_policy": null
    },
    "... 2 more trimmed ..."
  ],
  "conflicts": [
    {
      "id": "07bb8163-071d-4379-89f8-b7651ef976ee",
      "code": "RETAINED_WITH_PII",
      "severity": "WARNING",
      "schema_name": "app",
      "table_name": "purchase_order",
      "message": "app.purchase_order: column(s) shipping_name are pseudonymized, not anonymized; retained rows may still be re-identifiable",
      "resolution": null,
      "acknowledgeable": true
    },
    {
      "id": "7e70d6b0-6f6f-48a7-8720-18353adf9f89",
      "code": "CASCADE_DESTROYS_RETAINED",
      "severity": "BLOCKING",
      "schema_name": "app",
      "table_name": "ticket_message",
      "message": "app.ticket_message: cascade would delete a record not owned by the subject (1 row via FK ticket_message_ticket_id_fkey (ON DELETE CASCADE), NOT NULL column(s) ticket_id)",
      "resolution": null,
      "acknowledgeable": false
    },
    {
      "id": "21cba39a-8f11-4c26-95dd-be33b055915d",
      "code": "SHARED_RECORD",
      "severity": "BLOCKING",
      "schema_name": "app",
      "table_name": "ticket_message",
      "message": "app.ticket_message: 1 row shared with another subject reference rows being deleted through a NOT NULL column; deleting would destroy or block them. Change the policy or schema and re-plan",
      "resolution": null,
      "acknowledgeable": false
    },
    "... 2 more trimmed ..."
  ],
  "counts_by_category": {
    "INVALIDATE": 3,
    "RETAIN": 4,
    "DELETE": 7,
    "RECOMPUTE": 3,
    "SCHEDULE": 1,
    "MANUAL_REVIEW": 1
  },
  "is_blocked": true,
  "created_at": "2026-11-04T17:50:41.273651Z",
  "approved_by": null,
  "approved_at": null
}
```


### 5.2 `GET /api/erasure-plans/{plan_id}`
- **Auth:** token. **Response:** `PlanOut`. **Errors:** 404.

```bash
curl -s -X GET "http://127.0.0.1:8001/api/erasure-plans/c313dceb-2f83-413a-83d3-7c6ac8e52c8e" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver"
```

HTTP 200

```json
{
  "id": "c313dceb-2f83-413a-83d3-7c6ac8e52c8e",
  "request_id": "adca6a98-f40c-44c5-b9af-5dfd8f06a8df",
  "version": 1,
  "status": "DRAFT",
  "snapshot_id": "3aa0c5bf-5983-497d-a1d1-53f18b1532c3",
  "schema_fingerprint": "59ec6eeab9a2d0cb88d8accc9599bc76955c4e56af4d46a4f5d5b5bd893d4228",
  "record_set_hash": "4ae209a8cb1ff2dff65c5c9158514795c3e0f8b767a78cf7b82970ca435b3e36",
  "plan_hash": "51ead16db0e340daea7ccc87be359cc3b0e1f9c45e9b6fdaffb6690bef405a20",
  "record_count": 10,
  "items": [
    {
      "id": "50349190-80e4-4c66-a7eb-e42259f097bf",
      "step_order": 1,
      "action": "ANONYMIZE_ROWS",
      "category": "RETAIN",
      "schema_name": "app",
      "table_name": "purchase_order",
      "lineage_level": "RECORD",
      "key_columns": [
        "id"
      ],
      "record_count": 1,
      "record_key_hmacs": [
        "7f3e49a48d9e9549a0cb4bec28afcfc0cedbec955d36a11150834dcb742e080d"
      ],
      "target_columns": [
        "customer_id",
        "shipping_address",
        "shipping_name"
      ],
      "expected_row_count": 1,
      "depends_on_steps": [],
      "reason_edge_ids": [
        "5bd59208-f687-5d09-8888-3f22a4463886"
      ],
      "reason": "ANONYMIZE 1 row of app.purchase_order: retention policy ANONYMIZE; columns customer_id (NULLIFY: references rows being deleted), shipping_address (NULLIFY), shipping_name (PSEUDONYMIZE)",
      "retention_policy": "ANONYMIZE (tax record retention (synthetic))"
    },
    {
      "id": "0b1d19bd-bdaa-455d-9de4-e6fa30234025",
      "step_order": 2,
      "action": "DELETE_ROWS",
      "category": "DELETE",
      "schema_name": "app",
      "table_name": "loyalty_account",
      "lineage_level": "RECORD",
      "key_columns": [
        "customer_id"
      ],
      "record_count": 1,
      "record_key_hmacs": [
        "706659eb11f83a9248aad8fd0d6843774a56152b4b2b09e817f3c8d4e3f16522"
      ],
      "target_columns": [],
      "expected_row_count": 1,
      "depends_on_steps": [],
      "reason_edge_ids": [
        "0a692223-40e4-5bb5-b141-083211b87fae"
      ],
      "reason": "DELETE 1 row of app.loyalty_account: owned by subject via FK loyalty_account_customer_id_fkey (ON DELETE NO ACTION)",
      "retention_policy": null
    },
    {
      "id": "76c830e0-f248-4eb2-a6c6-83a813eb6e32",
      "step_order": 3,
      "action": "DELETE_ROWS",
      "category": "DELETE",
      "schema_name": "app",
      "table_name": "customer",
      "lineage_level": "RECORD",
      "key_columns": [
        "id"
      ],
      "record_count": 1,
      "record_key_hmacs": [
        "2aaa7fe4059402b96c44009e7dfaab37c35e9902c7a73fe3fc444ac3ccba1b60"
      ],
      "target_columns": [],
      "expected_row_count": 1,
      "depends_on_steps": [
        1,
        2
      ],
      "reason_edge_ids": [],
      "reason": "DELETE 1 row of app.customer: subject root; cascade_check customer_address_customer_id_fkey: app.customer_address(customer_id) <- (id) = 2; cascade_check document_customer_id_fkey: app.document(customer_id) <- (id) = 0",
      "retention_policy": null
    },
    "... 2 more trimmed ..."
  ],
  "conflicts": [
    {
      "id": "7e48f0f2-ac21-4c7b-921f-d312c9a062b2",
      "code": "RETAINED_WITH_PII",
      "severity": "WARNING",
      "schema_name": "app",
      "table_name": "purchase_order",
      "message": "app.purchase_order: column(s) shipping_name are pseudonymized, not anonymized; retained rows may still be re-identifiable",
      "resolution": null,
      "acknowledgeable": true
    },
    {
      "id": "8a7ab2c1-813f-47a2-8f99-57b19229b8e5",
      "code": "EXTERNAL_ARTIFACT",
      "severity": "WARNING",
      "schema_name": "external",
      "table_name": "crm_export_files",
      "message": "external.crm_export_files: external artifact crm_export_files is erased by a scheduled task and cannot be verified in the database",
      "resolution": null,
      "acknowledgeable": true
    }
  ],
  "counts_by_category": {
    "RETAIN": 2,
    "DELETE": 3,
    "INVALIDATE": 1,
    "RECOMPUTE": 3,
    "SCHEDULE": 1
  },
  "is_blocked": false,
  "created_at": "2026-11-04T17:50:35.255504Z",
  "approved_by": null,
  "approved_at": null
}
```


### 5.3 `POST /api/erasure-plans/{plan_id}/conflicts/{conflict_id}/acknowledge`
- **Auth:** token. **Request (optional):** `ConflictAcknowledge {note?: str ≤ 500}`. **Response:** `PlanOut` with a **new `plan_hash`**, because `resolved` is part of the hashed payload.
- Only `OPAQUE_ARTIFACT`, `RETAINED_WITH_PII`, `EXTERNAL_ARTIFACT` and `NO_PRIMARY_KEY` can be acknowledged (`planner.ACKNOWLEDGEABLE_CODES`). Allowed on DRAFT and BLOCKED plans. A BLOCKED plan becomes DRAFT once nothing blocking is left. Appends `CONFLICT_ACKNOWLEDGED` with `note_present` (a boolean, never the note text).
- **Errors:** 404 (unknown conflict), 409 (state), 409 `CONFLICT_NOT_ACKNOWLEDGEABLE`.

**Acknowledge EXTERNAL_ARTIFACT**

```bash
curl -s -X POST "http://127.0.0.1:8001/api/erasure-plans/c313dceb-2f83-413a-83d3-7c6ac8e52c8e/conflicts/8a7ab2c1-813f-47a2-8f99-57b19229b8e5/acknowledge" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver" -H "Content-Type: application/json" -d '{"note": "CRM export handled by scheduled task"}'
```

HTTP 200

```json
{
  "id": "c313dceb-2f83-413a-83d3-7c6ac8e52c8e",
  "request_id": "adca6a98-f40c-44c5-b9af-5dfd8f06a8df",
  "version": 1,
  "status": "DRAFT",
  "snapshot_id": "3aa0c5bf-5983-497d-a1d1-53f18b1532c3",
  "schema_fingerprint": "59ec6eeab9a2d0cb88d8accc9599bc76955c4e56af4d46a4f5d5b5bd893d4228",
  "record_set_hash": "4ae209a8cb1ff2dff65c5c9158514795c3e0f8b767a78cf7b82970ca435b3e36",
  "plan_hash": "c4b6584df9746f040445530b52b4ba4775e9f01593a99ee7246d00c590b5c8b8",
  "record_count": 10,
  "items": [
    {
      "id": "50349190-80e4-4c66-a7eb-e42259f097bf",
      "step_order": 1,
      "action": "ANONYMIZE_ROWS",
      "category": "RETAIN",
      "schema_name": "app",
      "table_name": "purchase_order",
      "lineage_level": "RECORD",
      "key_columns": [
        "id"
      ],
      "record_count": 1,
      "record_key_hmacs": [
        "7f3e49a48d9e9549a0cb4bec28afcfc0cedbec955d36a11150834dcb742e080d"
      ],
      "target_columns": [
        "customer_id",
        "shipping_address",
        "shipping_name"
      ],
      "expected_row_count": 1,
      "depends_on_steps": [],
      "reason_edge_ids": [
        "5bd59208-f687-5d09-8888-3f22a4463886"
      ],
      "reason": "ANONYMIZE 1 row of app.purchase_order: retention policy ANONYMIZE; columns customer_id (NULLIFY: references rows being deleted), shipping_address (NULLIFY), shipping_name (PSEUDONYMIZE)",
      "retention_policy": "ANONYMIZE (tax record retention (synthetic))"
    },
    {
      "id": "0b1d19bd-bdaa-455d-9de4-e6fa30234025",
      "step_order": 2,
      "action": "DELETE_ROWS",
      "category": "DELETE",
      "schema_name": "app",
      "table_name": "loyalty_account",
      "lineage_level": "RECORD",
      "key_columns": [
        "customer_id"
      ],
      "record_count": 1,
      "record_key_hmacs": [
        "706659eb11f83a9248aad8fd0d6843774a56152b4b2b09e817f3c8d4e3f16522"
      ],
      "target_columns": [],
      "expected_row_count": 1,
      "depends_on_steps": [],
      "reason_edge_ids": [
        "0a692223-40e4-5bb5-b141-083211b87fae"
      ],
      "reason": "DELETE 1 row of app.loyalty_account: owned by subject via FK loyalty_account_customer_id_fkey (ON DELETE NO ACTION)",
      "retention_policy": null
    },
    {
      "id": "76c830e0-f248-4eb2-a6c6-83a813eb6e32",
      "step_order": 3,
      "action": "DELETE_ROWS",
      "category": "DELETE",
      "schema_name": "app",
      "table_name": "customer",
      "lineage_level": "RECORD",
      "key_columns": [
        "id"
      ],
      "record_count": 1,
      "record_key_hmacs": [
        "2aaa7fe4059402b96c44009e7dfaab37c35e9902c7a73fe3fc444ac3ccba1b60"
      ],
      "target_columns": [],
      "expected_row_count": 1,
      "depends_on_steps": [
        1,
        2
      ],
      "reason_edge_ids": [],
      "reason": "DELETE 1 row of app.customer: subject root; cascade_check customer_address_customer_id_fkey: app.customer_address(customer_id) <- (id) = 2; cascade_check document_customer_id_fkey: app.document(customer_id) <- (id) = 0",
      "retention_policy": null
    },
    "... 2 more trimmed ..."
  ],
  "conflicts": [
    {
      "id": "7e48f0f2-ac21-4c7b-921f-d312c9a062b2",
      "code": "RETAINED_WITH_PII",
      "severity": "WARNING",
      "schema_name": "app",
      "table_name": "purchase_order",
      "message": "app.purchase_order: column(s) shipping_name are pseudonymized, not anonymized; retained rows may still be re-identifiable",
      "resolution": null,
      "acknowledgeable": true
    },
    {
      "id": "8a7ab2c1-813f-47a2-8f99-57b19229b8e5",
      "code": "EXTERNAL_ARTIFACT",
      "severity": "WARNING",
      "schema_name": "external",
      "table_name": "crm_export_files",
      "message": "external.crm_export_files: external artifact crm_export_files is erased by a scheduled task and cannot be verified in the database",
      "resolution": "ACKNOWLEDGED",
      "acknowledgeable": false
    }
  ],
  "counts_by_category": {
    "RETAIN": 2,
    "DELETE": 3,
    "INVALIDATE": 1,
    "RECOMPUTE": 3,
    "SCHEDULE": 1
  },
  "is_blocked": false,
  "created_at": "2026-11-04T17:50:35.255504Z",
  "approved_by": null,
  "approved_at": null
}
```

**Attempt to acknowledge SHARED_RECORD (never acknowledgeable)**

```bash
curl -s -X POST "http://127.0.0.1:8001/api/erasure-plans/42a51f85-d3a9-47ab-bb8a-da1049cc507f/conflicts/21cba39a-8f11-4c26-95dd-be33b055915d/acknowledge" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver"
```

HTTP 409

```json
{
  "error": {
    "code": "CONFLICT_NOT_ACKNOWLEDGEABLE",
    "message": "conflict SHARED_RECORD cannot be acknowledged; change the policy and re-plan",
    "details": {
      "index": 2,
      "code": "SHARED_RECORD",
      "table": "app.ticket_message"
    }
  }
}
```


### 5.4 `POST /api/erasure-plans/{plan_id}/approve`
- **Auth:** token. **Request:** `PlanApprove {plan_hash: ^[0-9a-f]{64}$}`. **Response:** `PlanOut` (status `APPROVED`).
- The stored plan is re-hashed first. Then the checks are: not blocked (409 `PLAN_BLOCKED`), the status allows APPROVED (409 `INVALID_STATE`), and the hash equals `plan_hash` (409 `PLAN_HASH_MISMATCH`). Appends `PLAN_APPROVED`.

```bash
curl -s -X POST "http://127.0.0.1:8001/api/erasure-plans/c313dceb-2f83-413a-83d3-7c6ac8e52c8e/approve" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver" -H "Content-Type: application/json" -d '{"plan_hash": "c4b6584df9746f040445530b52b4ba4775e9f01593a99ee7246d00c590b5c8b8"}'
```

HTTP 200

```json
{
  "id": "c313dceb-2f83-413a-83d3-7c6ac8e52c8e",
  "request_id": "adca6a98-f40c-44c5-b9af-5dfd8f06a8df",
  "version": 1,
  "status": "APPROVED",
  "snapshot_id": "3aa0c5bf-5983-497d-a1d1-53f18b1532c3",
  "schema_fingerprint": "59ec6eeab9a2d0cb88d8accc9599bc76955c4e56af4d46a4f5d5b5bd893d4228",
  "record_set_hash": "4ae209a8cb1ff2dff65c5c9158514795c3e0f8b767a78cf7b82970ca435b3e36",
  "plan_hash": "c4b6584df9746f040445530b52b4ba4775e9f01593a99ee7246d00c590b5c8b8",
  "record_count": 10,
  "items": [
    {
      "id": "50349190-80e4-4c66-a7eb-e42259f097bf",
      "step_order": 1,
      "action": "ANONYMIZE_ROWS",
      "category": "RETAIN",
      "schema_name": "app",
      "table_name": "purchase_order",
      "lineage_level": "RECORD",
      "key_columns": [
        "id"
      ],
      "record_count": 1,
      "record_key_hmacs": [
        "7f3e49a48d9e9549a0cb4bec28afcfc0cedbec955d36a11150834dcb742e080d"
      ],
      "target_columns": [
        "customer_id",
        "shipping_address",
        "shipping_name"
      ],
      "expected_row_count": 1,
      "depends_on_steps": [],
      "reason_edge_ids": [
        "5bd59208-f687-5d09-8888-3f22a4463886"
      ],
      "reason": "ANONYMIZE 1 row of app.purchase_order: retention policy ANONYMIZE; columns customer_id (NULLIFY: references rows being deleted), shipping_address (NULLIFY), shipping_name (PSEUDONYMIZE)",
      "retention_policy": "ANONYMIZE (tax record retention (synthetic))"
    },
    {
      "id": "0b1d19bd-bdaa-455d-9de4-e6fa30234025",
      "step_order": 2,
      "action": "DELETE_ROWS",
      "category": "DELETE",
      "schema_name": "app",
      "table_name": "loyalty_account",
      "lineage_level": "RECORD",
      "key_columns": [
        "customer_id"
      ],
      "record_count": 1,
      "record_key_hmacs": [
        "706659eb11f83a9248aad8fd0d6843774a56152b4b2b09e817f3c8d4e3f16522"
      ],
      "target_columns": [],
      "expected_row_count": 1,
      "depends_on_steps": [],
      "reason_edge_ids": [
        "0a692223-40e4-5bb5-b141-083211b87fae"
      ],
      "reason": "DELETE 1 row of app.loyalty_account: owned by subject via FK loyalty_account_customer_id_fkey (ON DELETE NO ACTION)",
      "retention_policy": null
    },
    {
      "id": "76c830e0-f248-4eb2-a6c6-83a813eb6e32",
      "step_order": 3,
      "action": "DELETE_ROWS",
      "category": "DELETE",
      "schema_name": "app",
      "table_name": "customer",
      "lineage_level": "RECORD",
      "key_columns": [
        "id"
      ],
      "record_count": 1,
      "record_key_hmacs": [
        "2aaa7fe4059402b96c44009e7dfaab37c35e9902c7a73fe3fc444ac3ccba1b60"
      ],
      "target_columns": [],
      "expected_row_count": 1,
      "depends_on_steps": [
        1,
        2
      ],
      "reason_edge_ids": [],
      "reason": "DELETE 1 row of app.customer: subject root; cascade_check customer_address_customer_id_fkey: app.customer_address(customer_id) <- (id) = 2; cascade_check document_customer_id_fkey: app.document(customer_id) <- (id) = 0",
      "retention_policy": null
    },
    "... 2 more trimmed ..."
  ],
  "conflicts": [
    {
      "id": "7e48f0f2-ac21-4c7b-921f-d312c9a062b2",
      "code": "RETAINED_WITH_PII",
      "severity": "WARNING",
      "schema_name": "app",
      "table_name": "purchase_order",
      "message": "app.purchase_order: column(s) shipping_name are pseudonymized, not anonymized; retained rows may still be re-identifiable",
      "resolution": null,
      "acknowledgeable": true
    },
    {
      "id": "8a7ab2c1-813f-47a2-8f99-57b19229b8e5",
      "code": "EXTERNAL_ARTIFACT",
      "severity": "WARNING",
      "schema_name": "external",
      "table_name": "crm_export_files",
      "message": "external.crm_export_files: external artifact crm_export_files is erased by a scheduled task and cannot be verified in the database",
      "resolution": "ACKNOWLEDGED",
      "acknowledgeable": false
    }
  ],
  "counts_by_category": {
    "RETAIN": 2,
    "DELETE": 3,
    "INVALIDATE": 1,
    "RECOMPUTE": 3,
    "SCHEDULE": 1
  },
  "is_blocked": false,
  "created_at": "2026-11-04T17:50:35.255504Z",
  "approved_by": "docs-approver",
  "approved_at": "2026-11-04T17:50:37.201078Z"
}
```

**Wrong hash**

```bash
curl -s -X POST "http://127.0.0.1:8001/api/erasure-plans/c313dceb-2f83-413a-83d3-7c6ac8e52c8e/approve" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver" -H "Content-Type: application/json" -d '{"plan_hash": "0000000000000000000000000000000000000000000000000000000000000000"}'
```

HTTP 409

```json
{
  "error": {
    "code": "PLAN_HASH_MISMATCH",
    "message": "approval hash does not match the plan hash",
    "details": {
      "plan_id": "c313dceb-2f83-413a-83d3-7c6ac8e52c8e"
    }
  }
}
```

**Blocked plan**

```bash
curl -s -X POST "http://127.0.0.1:8001/api/erasure-plans/42a51f85-d3a9-47ab-bb8a-da1049cc507f/approve" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver" -H "Content-Type: application/json" -d '{"plan_hash": "3925a26027207740d03e3958598a64f8bdd67bac54899959321c6085ae7aafa6"}'
```

HTTP 409

```json
{
  "error": {
    "code": "PLAN_BLOCKED",
    "message": "plan has unresolved blocking conflicts",
    "details": {
      "plan_id": "42a51f85-d3a9-47ab-bb8a-da1049cc507f",
      "unresolved": [
        "CASCADE_DESTROYS_RETAINED",
        "EXTERNAL_ARTIFACT",
        "RETAINED_WITH_PII",
        "... 1 more trimmed ..."
      ]
    }
  }
}
```


### 5.5 `POST /api/erasure-plans/{plan_id}/execute`
- **Auth:** token. **Request:** `PlanExecute {mode: "DRY_RUN"|"LIVE"}`. **Response:** `ExecutionOut {id, plan_id, mode, status, started_at, finished_at, steps: list[StepResultOut], error, verification, evidence_id, timings_ms}`.
- `DRY_RUN`: allowed on DRAFT or APPROVED plans. All steps run in a SERIALIZABLE transaction, which is then rolled back (status `ROLLED_BACK`, event `EXECUTION_ROLLED_BACK`).
- `LIVE`: requires `APPROVED` and no committed LIVE run, and the caller's `X-LAE-Actor` must differ from the approver's when `LAE_REQUIRE_FOUR_EYES` is true (the default). Otherwise it returns 409 `INVALID_STATE` with `details.rule = "four_eyes"`. The drift check, row locks and rowcount assertions all run in one SERIALIZABLE transaction. `EXECUTION_COMMITTED` is appended in the same transaction. Verification and evidence then run in later transactions and are returned in `verification` and `evidence_id`.
- **Errors:** 409 `INVALID_STATE` (not approved, already executed: see note), 409 `PLAN_BLOCKED`, `PLAN_HASH_MISMATCH`, `STALE_PLAN`, `ROW_COUNT_MISMATCH`, 503 `SERIALIZATION_CONFLICT`, 409 for other execution errors. On a LIVE error the run is recorded as `FAILED` with `EXECUTION_FAILED`, and the error `details` include `execution_run_id`.
- **Note (AT-17):** a second LIVE call on an executed plan returns **409 `INVALID_STATE`**, not `ALREADY_EXECUTED`, because the plan status is no longer `APPROVED` (see sample).

**LIVE before approval**

```bash
curl -s -X POST "http://127.0.0.1:8001/api/erasure-plans/c313dceb-2f83-413a-83d3-7c6ac8e52c8e/execute" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver" -H "Content-Type: application/json" -d '{"mode": "LIVE"}'
```

HTTP 409

```json
{
  "error": {
    "code": "INVALID_STATE",
    "message": "only an APPROVED plan can be executed LIVE",
    "details": {}
  }
}
```

**DRY_RUN (trimmed to 4 steps)**

```bash
curl -s -X POST "http://127.0.0.1:8001/api/erasure-plans/c313dceb-2f83-413a-83d3-7c6ac8e52c8e/execute" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver" -H "Content-Type: application/json" -d '{"mode": "DRY_RUN"}'
```

HTTP 200

```json
{
  "id": "5ff384f3-9d42-403b-b99f-df5343831d31",
  "plan_id": "c313dceb-2f83-413a-83d3-7c6ac8e52c8e",
  "mode": "DRY_RUN",
  "status": "ROLLED_BACK",
  "started_at": "2026-11-04T17:50:36.368054Z",
  "finished_at": "2026-11-04T17:50:37.029053Z",
  "steps": [
    {
      "step_order": 1,
      "action": "ANONYMIZE_ROWS",
      "category": "RETAIN",
      "table": "app.purchase_order",
      "status": "APPLIED",
      "rows_affected": 1,
      "expected_rows": 1,
      "duration_ms": 13
    },
    {
      "step_order": 2,
      "action": "DELETE_ROWS",
      "category": "DELETE",
      "table": "app.loyalty_account",
      "status": "APPLIED",
      "rows_affected": 1,
      "expected_rows": 1,
      "duration_ms": 7
    },
    {
      "step_order": 3,
      "action": "DELETE_ROWS",
      "category": "DELETE",
      "table": "app.customer",
      "status": "APPLIED",
      "rows_affected": 1,
      "expected_rows": 1,
      "duration_ms": 17
    },
    {
      "step_order": 4,
      "action": "EXPECT_CASCADE",
      "category": "DELETE",
      "table": "app.customer_address",
      "status": "APPLIED",
      "rows_affected": 2,
      "expected_rows": 2,
      "duration_ms": 9
    },
    "... 6 more trimmed ..."
  ],
  "error": null,
  "verification": null,
  "evidence_id": null,
  "timings_ms": {
    "execution": 487
  }
}
```

**LIVE by the approver (four-eyes rule)**

```bash
curl -s -X POST "http://127.0.0.1:8001/api/erasure-plans/c313dceb-2f83-413a-83d3-7c6ac8e52c8e/execute" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver" -H "Content-Type: application/json" -d '{"mode": "LIVE"}'
```

HTTP 409

```json
{
  "error": {
    "code": "INVALID_STATE",
    "message": "separation of duties: the approver may not execute the plan",
    "details": {
      "rule": "four_eyes"
    }
  }
}
```

**LIVE (subject 18): all 10 steps, verification trimmed**

```bash
curl -s -X POST "http://127.0.0.1:8001/api/erasure-plans/c313dceb-2f83-413a-83d3-7c6ac8e52c8e/execute" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-executor" -H "Content-Type: application/json" -d '{"mode": "LIVE"}'
```

HTTP 200

```json
{
  "id": "7f6f7afb-1a9c-475c-b35d-08c36279756f",
  "plan_id": "c313dceb-2f83-413a-83d3-7c6ac8e52c8e",
  "mode": "LIVE",
  "status": "COMMITTED",
  "started_at": "2026-11-04T17:50:37.473470Z",
  "finished_at": "2026-11-04T17:50:37.916874Z",
  "steps": [
    {
      "step_order": 1,
      "action": "ANONYMIZE_ROWS",
      "category": "RETAIN",
      "table": "app.purchase_order",
      "status": "APPLIED",
      "rows_affected": 1,
      "expected_rows": 1,
      "duration_ms": 9
    },
    {
      "step_order": 2,
      "action": "DELETE_ROWS",
      "category": "DELETE",
      "table": "app.loyalty_account",
      "status": "APPLIED",
      "rows_affected": 1,
      "expected_rows": 1,
      "duration_ms": 0
    },
    {
      "step_order": 3,
      "action": "DELETE_ROWS",
      "category": "DELETE",
      "table": "app.customer",
      "status": "APPLIED",
      "rows_affected": 1,
      "expected_rows": 1,
      "duration_ms": 12
    },
    {
      "step_order": 4,
      "action": "EXPECT_CASCADE",
      "category": "DELETE",
      "table": "app.customer_address",
      "status": "APPLIED",
      "rows_affected": 2,
      "expected_rows": 2,
      "duration_ms": 5
    },
    {
      "step_order": 5,
      "action": "INVALIDATE_CACHE",
      "category": "INVALIDATE",
      "table": "app.customer_profile_cache",
      "status": "APPLIED",
      "rows_affected": 1,
      "expected_rows": 1,
      "duration_ms": 7
    },
    {
      "step_order": 6,
      "action": "REFRESH_MATVIEW",
      "category": "RECOMPUTE",
      "table": "app.customer_ltv",
      "status": "APPLIED",
      "rows_affected": null,
      "expected_rows": null,
      "duration_ms": 16
    },
    {
      "step_order": 7,
      "action": "RECOMPUTE_TABLE",
      "category": "RECOMPUTE",
      "table": "app.customer_segment",
      "status": "APPLIED",
      "rows_affected": 14,
      "expected_rows": null,
      "duration_ms": 14
    },
    {
      "step_order": 8,
      "action": "REFRESH_MATVIEW",
      "category": "RECOMPUTE",
      "table": "app.daily_revenue",
      "status": "APPLIED",
      "rows_affected": null,
      "expected_rows": null,
      "duration_ms": 15
    },
    {
      "step_order": 9,
      "action": "RETAIN_ROWS",
      "category": "RETAIN",
      "table": "app.order_line",
      "status": "APPLIED",
      "rows_affected": 4,
      "expected_rows": 4,
      "duration_ms": 5
    },
    {
      "step_order": 10,
      "action": "SCHEDULE_EXTERNAL",
      "category": "SCHEDULE",
      "table": "external.crm_export_files",
      "status": "SKIPPED",
      "rows_affected": null,
      "expected_rows": 1,
      "duration_ms": 0
    }
  ],
  "error": null,
  "verification": {
    "id": "29fb1a0f-f1b0-42bb-902c-ccdaf5cad0d0",
    "execution_run_id": "7f6f7afb-1a9c-475c-b35d-08c36279756f",
    "status": "PASSED",
    "outcome": "PARTIAL",
    "required_artifacts": 10,
    "verified_artifacts": 9,
    "coverage": 0.9,
    "checks": [
      {
        "check_type": "CASCADE_COMPLETE",
        "schema_name": "app",
        "table_name": "customer_address",
        "column_name": null,
        "step_order": 4,
        "passed": true,
        "observed": {
          "remaining_rows": 0
        },
        "expected": {
          "planned_keys": 2,
          "remaining_rows": 0
        }
      },
      {
        "check_type": "COLUMNS_ANONYMIZED",
        "schema_name": "app",
        "table_name": "purchase_order",
        "column_name": "customer_id",
        "step_order": 1,
        "passed": true,
        "observed": {
          "violations": 0,
          "rows_present": 1
        },
        "expected": {
          "violations": 0,
          "rows_present": 1
        }
      },
      "... 11 more checks trimmed ..."
    ],
    "residual_hits": [],
    "started_at": "2026-11-04T17:50:38.019641Z",
    "finished_at": "2026-11-04T17:50:38.748038Z",
    "duration_ms": 728
  },
  "evidence_id": "a21ded73-047c-40d7-9805-a627b8c636c4",
  "timings_ms": {
    "execution": 479,
    "verification": 894,
    "evidence": 237
  }
}
```

**Second LIVE call**

```bash
curl -s -X POST "http://127.0.0.1:8001/api/erasure-plans/c313dceb-2f83-413a-83d3-7c6ac8e52c8e/execute" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-executor" -H "Content-Type: application/json" -d '{"mode": "LIVE"}'
```

HTTP 409

```json
{
  "error": {
    "code": "INVALID_STATE",
    "message": "only an APPROVED plan can be executed LIVE",
    "details": {}
  }
}
```


### 5.6 `GET /api/executions?request_id=`
- **Auth:** token. **Query:** `request_id` (required UUID). **Response:** `list[ExecutionOut]` across all plan versions, newest first (without `verification`).

```bash
curl -s -X GET "http://127.0.0.1:8001/api/executions?request_id=adca6a98-f40c-44c5-b9af-5dfd8f06a8df" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver"
```

HTTP 200

```json
[
  {
    "id": "7f6f7afb-1a9c-475c-b35d-08c36279756f",
    "plan_id": "c313dceb-2f83-413a-83d3-7c6ac8e52c8e",
    "mode": "LIVE",
    "status": "COMMITTED",
    "started_at": "2026-11-04T17:50:37.473470Z",
    "finished_at": "2026-11-04T17:50:37.916874Z",
    "steps": [
      {
        "step_order": 1,
        "action": "ANONYMIZE_ROWS",
        "category": "RETAIN",
        "table": "app.purchase_order",
        "status": "APPLIED",
        "rows_affected": 1,
        "expected_rows": 1,
        "duration_ms": 9
      },
      {
        "step_order": 2,
        "action": "DELETE_ROWS",
        "category": "DELETE",
        "table": "app.loyalty_account",
        "status": "APPLIED",
        "rows_affected": 1,
        "expected_rows": 1,
        "duration_ms": 0
      },
      "... 8 more trimmed ..."
    ],
    "error": null,
    "verification": {
      "id": "29fb1a0f-f1b0-42bb-902c-ccdaf5cad0d0",
      "execution_run_id": "7f6f7afb-1a9c-475c-b35d-08c36279756f",
      "status": "PASSED",
      "outcome": "PARTIAL",
      "required_artifacts": 10,
      "verified_artifacts": 9,
      "coverage": 0.9,
      "checks": [
        {
          "check_type": "CASCADE_COMPLETE",
          "schema_name": "app",
          "table_name": "customer_address",
          "column_name": null,
          "step_order": 4,
          "passed": true,
          "observed": {
            "remaining_rows": 0
          },
          "expected": {
            "planned_keys": 2,
            "remaining_rows": 0
          }
        },
        {
          "check_type": "COLUMNS_ANONYMIZED",
          "schema_name": "app",
          "table_name": "purchase_order",
          "column_name": "customer_id",
          "step_order": 1,
          "passed": true,
          "observed": {
            "violations": 0,
            "rows_present": 1
          },
          "expected": {
            "violations": 0,
            "rows_present": 1
          }
        },
        "... 1 more trimmed ..."
      ],
      "residual_hits": [],
      "started_at": "2026-11-04T17:50:38.019641Z",
      "finished_at": "2026-11-04T17:50:38.748038Z",
      "duration_ms": 728
    },
    "evidence_id": "a21ded73-047c-40d7-9805-a627b8c636c4",
    "timings_ms": {}
  },
  {
    "id": "5ff384f3-9d42-403b-b99f-df5343831d31",
    "plan_id": "c313dceb-2f83-413a-83d3-7c6ac8e52c8e",
    "mode": "DRY_RUN",
    "status": "ROLLED_BACK",
    "started_at": "2026-11-04T17:50:36.368054Z",
    "finished_at": "2026-11-04T17:50:37.029053Z",
    "steps": [
      {
        "step_order": 1,
        "action": "ANONYMIZE_ROWS",
        "category": "RETAIN",
        "table": "app.purchase_order",
        "status": "APPLIED",
        "rows_affected": 1,
        "expected_rows": 1,
        "duration_ms": 13
      },
      {
        "step_order": 2,
        "action": "DELETE_ROWS",
        "category": "DELETE",
        "table": "app.loyalty_account",
        "status": "APPLIED",
        "rows_affected": 1,
        "expected_rows": 1,
        "duration_ms": 7
      },
      "... 8 more trimmed ..."
    ],
    "error": null,
    "verification": null,
    "evidence_id": null,
    "timings_ms": {}
  }
]
```


---

## 6. Verification

### 6.1 `GET /api/verification/{request_id}`
- **Auth:** token. **Response:** `VerificationOut {id, execution_run_id, status, outcome, required_artifacts, verified_artifacts, coverage (float, display only), checks: list[CheckOut], residual_hits: list[ResidualHitOut {table, column, count}], started_at, finished_at, duration_ms}`. This is the latest verification of the latest committed LIVE run. **Errors:** 404 if none exists.
- `residual_hits[].count` is a **match count summed over normalizations**, not a row count (CR verification-crypto-engineer-3 §4).

**Subject 18: PARTIAL, 9/10 (external artifact not verifiable)**

```bash
curl -s -X GET "http://127.0.0.1:8001/api/verification/adca6a98-f40c-44c5-b9af-5dfd8f06a8df" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver"
```

HTTP 200

```json
{
  "id": "29fb1a0f-f1b0-42bb-902c-ccdaf5cad0d0",
  "execution_run_id": "7f6f7afb-1a9c-475c-b35d-08c36279756f",
  "status": "PASSED",
  "outcome": "PARTIAL",
  "required_artifacts": 10,
  "verified_artifacts": 9,
  "coverage": 0.9,
  "checks": [
    {
      "check_type": "CASCADE_COMPLETE",
      "schema_name": "app",
      "table_name": "customer_address",
      "column_name": null,
      "step_order": 4,
      "passed": true,
      "observed": {
        "remaining_rows": 0
      },
      "expected": {
        "planned_keys": 2,
        "remaining_rows": 0
      }
    },
    {
      "check_type": "REDISCOVERY_EMPTY",
      "schema_name": null,
      "table_name": null,
      "column_name": null,
      "step_order": null,
      "passed": true,
      "observed": {
        "retained_records": 0,
        "remaining_records": 0,
        "remaining_by_table": [],
        "subject_root_found": false
      },
      "expected": {
        "remaining_records": 0
      }
    },
    {
      "check_type": "RESIDUAL_VALUE_SCAN",
      "schema_name": null,
      "table_name": null,
      "column_name": null,
      "step_order": null,
      "passed": true,
      "observed": {
        "hits": [],
        "total_hits": 0,
        "probe_count": 5,
        "retained_hits": [],
        "columns_scanned": 26
      },
      "expected": {
        "total_hits": 0
      }
    },
    "... 1 more trimmed ..."
  ],
  "residual_hits": [],
  "started_at": "2026-11-04T17:50:38.019641Z",
  "finished_at": "2026-11-04T17:50:38.748038Z",
  "duration_ms": 728
}
```


### 6.2 `POST /api/verification/{request_id}/run`
- **Auth:** token. **Response:** `VerificationOut` of a **new** verification run of the same committed execution. Appends `VERIFICATION_COMPLETED`, and feeds residual hits back as SUGGESTED edges.

```bash
curl -s -X POST "http://127.0.0.1:8001/api/verification/adca6a98-f40c-44c5-b9af-5dfd8f06a8df/run" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver"
```

HTTP 200

```json
{
  "id": "4ac2ae9a-985a-4137-9049-3369d0875411",
  "execution_run_id": "7f6f7afb-1a9c-475c-b35d-08c36279756f",
  "status": "PASSED",
  "outcome": "PARTIAL",
  "required_artifacts": 10,
  "verified_artifacts": 9,
  "coverage": 0.9,
  "checks": [
    {
      "check_type": "CASCADE_COMPLETE",
      "schema_name": "app",
      "table_name": "customer_address",
      "column_name": null,
      "step_order": 4,
      "passed": true,
      "observed": {
        "remaining_rows": 0
      },
      "expected": {
        "planned_keys": 2,
        "remaining_rows": 0
      }
    },
    {
      "check_type": "REDISCOVERY_EMPTY",
      "schema_name": null,
      "table_name": null,
      "column_name": null,
      "step_order": null,
      "passed": true,
      "observed": {
        "retained_records": 0,
        "remaining_records": 0,
        "remaining_by_table": [],
        "subject_root_found": false
      },
      "expected": {
        "remaining_records": 0
      }
    },
    {
      "check_type": "RESIDUAL_VALUE_SCAN",
      "schema_name": null,
      "table_name": null,
      "column_name": null,
      "step_order": null,
      "passed": true,
      "observed": {
        "hits": [],
        "total_hits": 0,
        "probe_count": 5,
        "retained_hits": [],
        "columns_scanned": 26
      },
      "expected": {
        "total_hits": 0
      }
    },
    "... 1 more trimmed ..."
  ],
  "residual_hits": [],
  "started_at": "2026-11-04T17:50:39.530885Z",
  "finished_at": "2026-11-04T17:50:40.169355Z",
  "duration_ms": 638
}
```


### 6.3 Sample: hidden copies make verification FAIL (subject 9)
Subject 9's e-mail is copied into `app.marketing_event_log` with no FK. The plan cannot see the copies, so the lineage-independent scan finds them.

```bash
curl -s -X POST "http://127.0.0.1:8001/api/erasure-plans/d281bf59-5e1b-477d-88db-20a92c441504/execute" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-executor" -H "Content-Type: application/json" -d '{"mode": "LIVE"}'
```

HTTP 200

```json
{
  "id": "c2b1188e-eb15-4fd7-997d-b02ac35cc7db",
  "plan_id": "d281bf59-5e1b-477d-88db-20a92c441504",
  "mode": "LIVE",
  "status": "COMMITTED",
  "started_at": "2026-11-04T17:50:43.312682Z",
  "finished_at": "2026-11-04T17:50:43.810482Z",
  "steps": [
    {
      "step_order": 1,
      "action": "ANONYMIZE_ROWS",
      "category": "RETAIN",
      "table": "app.purchase_order",
      "status": "APPLIED",
      "rows_affected": 1,
      "expected_rows": 1,
      "duration_ms": 7
    },
    {
      "step_order": 2,
      "action": "DELETE_ROWS",
      "category": "DELETE",
      "table": "app.customer",
      "status": "APPLIED",
      "rows_affected": 1,
      "expected_rows": 1,
      "duration_ms": 15
    },
    {
      "step_order": 3,
      "action": "EXPECT_CASCADE",
      "category": "DELETE",
      "table": "app.customer_address",
      "status": "APPLIED",
      "rows_affected": 1,
      "expected_rows": 1,
      "duration_ms": 0
    },
    "... 6 more trimmed ..."
  ],
  "error": null,
  "verification": {
    "id": "a19b7566-1abb-41ca-8232-684879667b36",
    "execution_run_id": "c2b1188e-eb15-4fd7-997d-b02ac35cc7db",
    "status": "FAILED",
    "outcome": "FAILED",
    "required_artifacts": 9,
    "verified_artifacts": 8,
    "coverage": 0.8888888888888888,
    "checks": [
      {
        "check_type": "CASCADE_COMPLETE",
        "schema_name": "app",
        "table_name": "customer_address",
        "column_name": null,
        "step_order": 3,
        "passed": true,
        "observed": {
          "remaining_rows": 0
        },
        "expected": {
          "planned_keys": 1,
          "remaining_rows": 0
        }
      },
      {
        "check_type": "COLUMNS_ANONYMIZED",
        "schema_name": "app",
        "table_name": "purchase_order",
        "column_name": "customer_id",
        "step_order": 1,
        "passed": true,
        "observed": {
          "violations": 0,
          "rows_present": 1
        },
        "expected": {
          "violations": 0,
          "rows_present": 1
        }
      },
      "... 10 more checks trimmed ..."
    ],
    "residual_hits": [
      {
        "table": "app.marketing_event_log",
        "column": "email",
        "count": 6
      }
    ],
    "started_at": "2026-11-04T17:50:43.891931Z",
    "finished_at": "2026-11-04T17:50:44.637662Z",
    "duration_ms": 745
  },
  "evidence_id": "65132bd5-6bdb-41a3-b75e-0ad6bfd4f62d",
  "timings_ms": {
    "execution": 506,
    "verification": 935,
    "evidence": 200
  }
}
```


### 6.4 Sample: after confirming the suggested edge (subject 6)
Once `customer.email → marketing_event_log.email` is CONFIRMED, the next plan contains `DELETE_ROWS` on `app.marketing_event_log` by predicate (no PK) and the residual scan passes:

```bash
curl -s -X POST "http://127.0.0.1:8001/api/erasure-plans/eaf36bf4-e40f-4698-a471-1a4de4d2898d/execute" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-executor" -H "Content-Type: application/json" -d '{"mode": "LIVE"}'
```

HTTP 200

```json
{
  "id": "f4ac1668-11d1-4311-8e1d-02c63e2a650f",
  "plan_id": "eaf36bf4-e40f-4698-a471-1a4de4d2898d",
  "mode": "LIVE",
  "status": "COMMITTED",
  "started_at": "2026-11-04T17:50:46.464307Z",
  "finished_at": "2026-11-04T17:50:47.020344Z",
  "steps": [
    {
      "step_order": 1,
      "action": "ANONYMIZE_ROWS",
      "category": "RETAIN",
      "table": "app.purchase_order",
      "status": "APPLIED",
      "rows_affected": 2,
      "expected_rows": 2,
      "duration_ms": 13
    },
    {
      "step_order": 2,
      "action": "DELETE_ROWS",
      "category": "DELETE",
      "table": "app.loyalty_account",
      "status": "APPLIED",
      "rows_affected": 1,
      "expected_rows": 1,
      "duration_ms": 5
    },
    {
      "step_order": 3,
      "action": "DELETE_ROWS",
      "category": "DELETE",
      "table": "app.customer",
      "status": "APPLIED",
      "rows_affected": 1,
      "expected_rows": 1,
      "duration_ms": 11
    },
    {
      "step_order": 4,
      "action": "DELETE_ROWS",
      "category": "DELETE",
      "table": "app.marketing_event_log",
      "status": "APPLIED",
      "rows_affected": 2,
      "expected_rows": 2,
      "duration_ms": 8
    },
    {
      "step_order": 5,
      "action": "DELETE_ROWS",
      "category": "DELETE",
      "table": "app.support_ticket",
      "status": "APPLIED",
      "rows_affected": 1,
      "expected_rows": 1,
      "duration_ms": 10
    },
    "... 9 more trimmed ..."
  ],
  "error": null,
  "verification": {
    "id": "05343efa-b8b5-441e-8445-328c568b4c93",
    "execution_run_id": "f4ac1668-11d1-4311-8e1d-02c63e2a650f",
    "status": "PASSED",
    "outcome": "PARTIAL",
    "required_artifacts": 14,
    "verified_artifacts": 13,
    "coverage": 0.9285714285714286,
    "checks": [
      {
        "check_type": "CASCADE_COMPLETE",
        "schema_name": "app",
        "table_name": "document",
        "column_name": null,
        "step_order": 6,
        "passed": true,
        "observed": {
          "remaining_rows": 0
        },
        "expected": {
          "planned_keys": 1,
          "remaining_rows": 0
        }
      },
      {
        "check_type": "CASCADE_COMPLETE",
        "schema_name": "app",
        "table_name": "ticket_message",
        "column_name": null,
        "step_order": 7,
        "passed": true,
        "observed": {
          "remaining_rows": 0
        },
        "expected": {
          "planned_keys": 1,
          "remaining_rows": 0
        }
      },
      "... 16 more checks trimmed ..."
    ],
    "residual_hits": [],
    "started_at": "2026-11-04T17:50:47.104389Z",
    "finished_at": "2026-11-04T17:50:47.837453Z",
    "duration_ms": 733
  },
  "evidence_id": "cb2e5504-3163-4473-bd45-04790493f718",
  "timings_ms": {
    "execution": 560,
    "verification": 879,
    "evidence": 167
  }
}
```


---

## 7. Audit and evidence

### 7.1 `GET /api/audit/events?after_seq=&limit=`
- **Auth:** token. **Query:** `after_seq ≥ 0` (default 0), `limit` 1–1000 (default 200). **Response:** `list[AuditEventOut {seq, event_id, occurred_at, event_type, entity_type, entity_id, actor, payload, prev_hash, event_hash}]`. `payload` is the `data` part of the stored envelope. `event_id` is **not** the stored `lae.audit_event.event_id`: `services/impl.py: _event_uuid` returns `UUID(event_hash[:32])`, as the samples show.

```bash
curl -s -X GET "http://127.0.0.1:8001/api/audit/events?after_seq=0&limit=3" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver"
```

HTTP 200

```json
[
  {
    "seq": 2,
    "event_id": "ac854374-b441-3787-1ed7-fc66f97e0684",
    "occurred_at": "2026-11-04T17:47:34.746300Z",
    "event_type": "SNAPSHOT_CREATED",
    "entity_type": "lineage_snapshot",
    "entity_id": "3aa0c5bf-5983-497d-a1d1-53f18b1532c3",
    "actor": "e2e-operator",
    "payload": {
      "fk_edges": 25,
      "inferred_edges": 2,
      "schema_fingerprint": "59ec6eeab9a2d0cb88d8accc9599bc76955c4e56af4d46a4f5d5b5bd893d4228",
      "sql_edges": 18,
      "tables": 17,
      "warnings": 0
    },
    "prev_hash": "0000000000000000000000000000000000000000000000000000000000000000",
    "event_hash": "ac854374b44137871ed7fc66f97e0684168d5f327e84ef908c0a4b30df62f634"
  },
  {
    "seq": 3,
    "event_id": "26ca126c-de33-bce6-572b-648c1afa810c",
    "occurred_at": "2026-11-04T17:47:34.787730Z",
    "event_type": "REQUEST_RECEIVED",
    "entity_type": "erasure_request",
    "entity_id": "a3997deb-f451-4e66-bbfd-0be528ff260b",
    "actor": "e2e-operator",
    "payload": {
      "subject_key_hmac": "e16a74268946eed68a47dfed8f07469abebc37df72de73f01755bdff31688fa5",
      "subject_root": "customer"
    },
    "prev_hash": "ac854374b44137871ed7fc66f97e0684168d5f327e84ef908c0a4b30df62f634",
    "event_hash": "26ca126cde33bce6572b648c1afa810c8930899af90fd79f77f359aae66ed07b"
  },
  {
    "seq": 4,
    "event_id": "e30ffa0a-a44a-fce1-3d7e-0bfca9a7be66",
    "occurred_at": "2026-11-04T17:47:34.950024Z",
    "event_type": "DISCOVERY_COMPLETED",
    "entity_type": "erasure_request",
    "entity_id": "a3997deb-f451-4e66-bbfd-0be528ff260b",
    "actor": "e2e-operator",
    "payload": {
      "affected_artifacts": 1,
      "duration_ms": 110,
      "record_set_hash": "4ae209a8cb1ff2dff65c5c9158514795c3e0f8b767a78cf7b82970ca435b3e36",
      "records": 10,
      "snapshot_id": "3aa0c5bf-5983-497d-a1d1-53f18b1532c3"
    },
    "prev_hash": "26ca126cde33bce6572b648c1afa810c8930899af90fd79f77f359aae66ed07b",
    "event_hash": "e30ffa0aa44afce13d7e0bfca9a7be6603fda1c4ecf8d9e48129fa60c87b19f7"
  }
]
```


### 7.2 `POST /api/audit/verify-chain`
- **Auth:** token (the route is read-only, but it sits behind the router-level token dependency). **Response:** `ChainVerificationOut {valid, checked_events, head_seq, head_hash, first_invalid_seq, reason}`. It recomputes the whole chain and also checks the envelope columns (reason `ENVELOPE_MISMATCH`). It appends nothing.

```bash
curl -s -X POST "http://127.0.0.1:8001/api/audit/verify-chain" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver"
```

HTTP 200

```json
{
  "valid": true,
  "checked_events": 75,
  "head_seq": 76,
  "head_hash": "a229d2e5cde111528154a1e872c4d88ffb045a026b160d8441b59eb9e4ff19fa",
  "first_invalid_seq": null,
  "reason": null
}
```


### 7.3 `GET /api/audit/{request_id}`
- **Auth:** token. **Response:** `AuditOut {request_id, events (of the request, its plans and runs), chain: ChainVerificationOut, evidence: EvidenceOut | null}`. `EvidenceOut` includes `signature_valid`, which is re-checked against the stored public key.

```bash
curl -s -X GET "http://127.0.0.1:8001/api/audit/adca6a98-f40c-44c5-b9af-5dfd8f06a8df" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver"
```

HTTP 200

```json
{
  "request_id": "adca6a98-f40c-44c5-b9af-5dfd8f06a8df",
  "events": [
    {
      "seq": 64,
      "event_id": "fa8b9e6d-ad2d-d656-3f37-dfcc4c95b73e",
      "occurred_at": "2026-11-04T17:50:33.519674Z",
      "event_type": "REQUEST_RECEIVED",
      "entity_type": "erasure_request",
      "entity_id": "adca6a98-f40c-44c5-b9af-5dfd8f06a8df",
      "actor": "docs-approver",
      "payload": {
        "subject_key_hmac": "2aaa7fe4059402b96c44009e7dfaab37c35e9902c7a73fe3fc444ac3ccba1b60",
        "subject_root": "customer"
      },
      "prev_hash": "f168370d7f1ba9b6ee514c9c261dbc58ae108445346779e9a25949149693bc75",
      "event_hash": "fa8b9e6dad2dd6563f37dfcc4c95b73e355f45dcfb8ced0798625d2fde3c5c5d"
    },
    {
      "seq": 65,
      "event_id": "39154e1b-ec06-6230-113b-fea7f2f1bd11",
      "occurred_at": "2026-11-04T17:50:34.511787Z",
      "event_type": "DISCOVERY_COMPLETED",
      "entity_type": "erasure_request",
      "entity_id": "adca6a98-f40c-44c5-b9af-5dfd8f06a8df",
      "actor": "docs-approver",
      "payload": {
        "affected_artifacts": 1,
        "duration_ms": 618,
        "record_set_hash": "4ae209a8cb1ff2dff65c5c9158514795c3e0f8b767a78cf7b82970ca435b3e36",
        "records": 10,
        "snapshot_id": "3aa0c5bf-5983-497d-a1d1-53f18b1532c3"
      },
      "prev_hash": "fa8b9e6dad2dd6563f37dfcc4c95b73e355f45dcfb8ced0798625d2fde3c5c5d",
      "event_hash": "39154e1bec066230113bfea7f2f1bd11e363cad1b1da1023c9134e1aba85a9c2"
    },
    "... 9 more events trimmed ..."
  ],
  "chain": {
    "valid": true,
    "checked_events": 75,
    "head_seq": 76,
    "head_hash": "a229d2e5cde111528154a1e872c4d88ffb045a026b160d8441b59eb9e4ff19fa",
    "first_invalid_seq": null,
    "reason": null
  },
  "evidence": {
    "id": "a21ded73-047c-40d7-9805-a627b8c636c4",
    "plan_id": "c313dceb-2f83-413a-83d3-7c6ac8e52c8e",
    "merkle_root": "30bf1be7c7b4beb8bdb3ff78c3cd238bb31cb959cf00276a09876824c5db5175",
    "leaf_count": 14,
    "chain_anchor_seq": 73,
    "chain_anchor_hash": "f8b1d2463019755965cb2a2bcc574e4c850a77ecca7576592e6628e9b81c1150",
    "signing_key_id": "c5ef0ed6ca133d1a",
    "issued_at": "2026-11-04T17:50:39.018860Z",
    "signature_valid": true,
    "bundle": "... same object as /api/evidence/{request_id}/download 'bundle' ...",
    "signature": "dw8bYHF966tpi2w6AKB3usB9FEMh4zh1cMcTV9Fd7sZebcWnRR4nCrkhJbLv8hV1eQuHkfjXSwzLextfbTnQAg=="
  }
}
```


### 7.4 `GET /api/evidence/{request_id}/download`
- **Auth:** token. **Response:** `application/json` attachment `evidence-<id>.json`. This is the canonical export file `{"bundle": {...}, "signature": "<base64 Ed25519>"}`. **Errors:** 404 if no evidence has been issued.

```bash
curl -s -X GET "http://127.0.0.1:8001/api/evidence/adca6a98-f40c-44c5-b9af-5dfd8f06a8df/download" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver"
```

HTTP 200

```json
{
  "bundle": {
    "bundle_version": "1",
    "chain_anchor": {
      "event_hash": "f8b1d2463019755965cb2a2bcc574e4c850a77ecca7576592e6628e9b81c1150",
      "seq": 73
    },
    "execution": {
      "committed_at": "2026-11-04T17:50:37.916874Z",
      "mode": "LIVE",
      "run_id": "7f6f7afb-1a9c-475c-b35d-08c36279756f",
      "step_counts": {
        "APPLIED": 9,
        "FAILED": 0,
        "SKIPPED": 1
      }
    },
    "issued_at": "2026-11-04T17:50:39.018860Z",
    "leaf_count": 14,
    "merkle_root": "30bf1be7c7b4beb8bdb3ff78c3cd238bb31cb959cf00276a09876824c5db5175",
    "plan_hash": "c4b6584df9746f040445530b52b4ba4775e9f01593a99ee7246d00c590b5c8b8",
    "plan_id": "c313dceb-2f83-413a-83d3-7c6ac8e52c8e",
    "record_set_hash": "4ae209a8cb1ff2dff65c5c9158514795c3e0f8b767a78cf7b82970ca435b3e36",
    "signing_key_id": "c5ef0ed6ca133d1a",
    "snapshot_fingerprint": "59ec6eeab9a2d0cb88d8accc9599bc76955c4e56af4d46a4f5d5b5bd893d4228",
    "verification": {
      "checks": [
        {
          "check_type": "CASCADE_COMPLETE",
          "column": null,
          "expected": {
            "planned_keys": 2,
            "remaining_rows": 0
          },
          "observed": {
            "remaining_rows": 0
          },
          "passed": true,
          "step_order": 4,
          "table": "app.customer_address"
        },
        "... 12 more checks trimmed ..."
      ],
      "status": "PASSED"
    }
  },
  "signature": "dw8bYHF966tpi2w6AKB3usB9FEMh4zh1cMcTV9Fd7sZebcWnRR4nCrkhJbLv8hV1eQuHkfjXSwzLextfbTnQAg=="
}
```


### 7.5 `GET /api/evidence/{request_id}/leaves`
- **Auth:** token. **Response:** `application/json` attachment `leaves-<id>.json`. This is the array of evidence leaves `{table, key_hmac, action, check_types, passed}` in bundle (Merkle) order.

```bash
curl -s -X GET "http://127.0.0.1:8001/api/evidence/adca6a98-f40c-44c5-b9af-5dfd8f06a8df/leaves" -H "X-LAE-Token: <token>" -H "X-LAE-Actor: docs-approver"
```

HTTP 200

```json
[
  {
    "action": "ANONYMIZE_ROWS",
    "check_types": [
      "COLUMNS_ANONYMIZED"
    ],
    "key_hmac": "7f3e49a48d9e9549a0cb4bec28afcfc0cedbec955d36a11150834dcb742e080d",
    "passed": true,
    "table": "app.purchase_order"
  },
  {
    "action": "DELETE_ROWS",
    "check_types": [
      "ROW_ABSENT"
    ],
    "key_hmac": "2aaa7fe4059402b96c44009e7dfaab37c35e9902c7a73fe3fc444ac3ccba1b60",
    "passed": true,
    "table": "app.customer"
  },
  {
    "action": "DELETE_ROWS",
    "check_types": [
      "ROW_ABSENT"
    ],
    "key_hmac": "706659eb11f83a9248aad8fd0d6843774a56152b4b2b09e817f3c8d4e3f16522",
    "passed": true,
    "table": "app.loyalty_account"
  },
  "... 11 more trimmed ..."
]
```


**Offline check (real run, no database involved):**

```
> lae verify-evidence bundle.json --public-key k.pub.pem --leaves leaves.json
{"chain_ok": null, "errors": [], "merkle_ok": true, "signature_ok": true, "valid": true}      (exit 0)
> # same bundle with chain_anchor.seq changed from 73 to 72 (tampered.json)
{"chain_ok": null, "errors": ["signature is invalid"], "merkle_ok": true, "signature_ok": false, "valid": false}   (exit 1)
```

---

## 8. Endpoint summary (all 23 paths)

| # | Method | Path | Auth | Request | Response |
|---|---|---|---|---|---|
| 1 | GET | `/api/health` | none | – | HealthOut |
| 2 | GET | `/api/stats` | token | – | StatsOut |
| 3 | GET | `/api/customers` | token | q, limit | list[CustomerOut] |
| 4 | GET | `/api/benchmarks` | token | scenario | list[BenchmarkRunOut] |
| 5 | POST | `/api/erasure-requests` | token | ErasureRequestCreate | 201 ErasureRequestOut |
| 6 | GET | `/api/erasure-requests` | token | limit | list[ErasureRequestOut] |
| 7 | GET | `/api/erasure-requests/{request_id}` | token | – | ErasureRequestOut |
| 8 | POST | `/api/discovery/{request_id}` | token | – | DiscoveryOut |
| 9 | GET | `/api/discovery/{request_id}` | token | – | DiscoveryOut (re-computed) |
| 10 | GET | `/api/lineage/schema` | token | level | SchemaGraphOut |
| 11 | GET | `/api/lineage/{request_id}` | token | level | LineageOut |
| 12 | POST | `/api/lineage-edges/{edge_id}/status` | token | EdgeStatusUpdate | GraphEdgeOut |
| 13 | POST | `/api/erasure-plans/{request_id}/generate` | token | PlanGenerate? | 201 PlanOut |
| 14 | GET | `/api/erasure-plans/{plan_id}` | token | – | PlanOut |
| 15 | POST | `/api/erasure-plans/{plan_id}/conflicts/{conflict_id}/acknowledge` | token | ConflictAcknowledge? | PlanOut |
| 16 | POST | `/api/erasure-plans/{plan_id}/approve` | token | PlanApprove | PlanOut |
| 17 | POST | `/api/erasure-plans/{plan_id}/execute` | token | PlanExecute | ExecutionOut |
| 18 | GET | `/api/executions` | token | request_id | list[ExecutionOut] |
| 19 | GET | `/api/verification/{request_id}` | token | – | VerificationOut |
| 20 | POST | `/api/verification/{request_id}/run` | token | – | VerificationOut |
| 21 | GET | `/api/audit/events` | token | after_seq, limit | list[AuditEventOut] |
| 22 | POST | `/api/audit/verify-chain` | token | – | ChainVerificationOut |
| 23 | GET | `/api/audit/{request_id}` | token | – | AuditOut |
| 24 | GET | `/api/evidence/{request_id}/download` | token | – | JSON file {bundle, signature} |
| 25 | GET | `/api/evidence/{request_id}/leaves` | token | – | JSON file [leaf, ...] |

25 operations on 23 paths (`/api/erasure-requests` and `/api/discovery/{request_id}` each have GET and POST).

