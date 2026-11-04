# Implementation Checklist

Legend: `[x]` done and verified by the Lead (test output recorded) · `[~]` in progress · `[ ]` not started · `[!]` blocked.
Rule: an item is ticked only when an automated test has passed against the real artefact. For DB items, that means the real PostgreSQL in Docker.

## Environment
- [x] Git repository (`main`)
- [x] Python venv + backend packages
- [x] Docker Desktop 4.98 / Engine 29.9.2 / Compose v5.6.1 (the CLI is not on PATH in agent shells; use the full path)
- [x] PostgreSQL 16.15 + pgvector 0.8.7 + pgcrypto 1.3 via `docker compose up -d db` (localhost:5433, DBs `lae` + `lae_test`, role `lae_reader`)

## Contracts
- [x] M0 contracts (8e45cdf), canonical JSON (81b5696)
- [x] CR graph-engineer-1, CR verification-crypto-engineer-1 decided
- [x] CR lead-1: action categories, outcome/coverage, CACHE/EXTERNAL artifacts, `/api` paths, token auth (329c357, 10694e5, a6975d0)
- [x] API contract: FastAPI app + schemas + service Protocol + OpenAPI (42d0bde). 6 API unit tests

## Stages (mission brief §18)
| Stage | Owner | Status | Evidence |
|---|---|---|---|
| 0 Inspect + verify reported state | Lead | [x] | 288 tests reproduced independently |
| 1 Preserve graph + crypto | Lead | [x] | golden vectors byte-identical through every commit |
| 2 DB schema, migrations, synthetic data, adapters | Database Eng | [x] 5f5c6f0 | 183 integration tests (real PG). Dev DB: 1 000 customers, 3 000 orders, 1 000 tickets, 500 documents, 500 embeddings |
| 3 Schema + FK discovery | Discovery Eng | [x] f04c921, 7967d07 | 12/12 FKs vs pg_get_constraintdef. Fingerprint + drift on real PG |
| 4 SQL lineage (SQLGlot) | Discovery Eng | [x] | 55-case matrix. Real viewdefs: customer_ltv/customer_segment KEY_PRESERVING, daily_revenue AGGREGATE |
| 5 Record-level resolution | Discovery Eng | [x] | precision = recall = F1 = 1 vs ground truth at tiny/small/default scale (FK + registered). Hidden copies missed by design |
| 6 Lineage graph + facade | Graph Eng | [x] 96faef2 | 75 tests, 100 % coverage |
| 7 Planner + policy engine | Erasure Eng | [x] a160fe4, 7967d07 | 170 tests, 99 %. Shared rows never destroyed (Hypothesis property) |
| 8 Safe execution | Erasure Eng + Lead | [x] | real PG: dry run leaves counts unchanged, drift gives STALE_PLAN with no changes, idempotent, all-or-nothing |
| 9 Independent verification | Verification Eng | [x] 8377766, e7a59c9 | 24 real-PG tests. Residual scan catches the hidden copies FK erasure leaves |
| 10 DB-backed audit + evidence | Verification Eng + DB Eng + Lead | [x] | offline verification with DB access blocked. Tamper detected at exact seq (hash + envelope) |
| 11 FastAPI integration | Lead | [x] 7967d07 | 23 endpoints. 9 e2e API tests on real PG with direct-SQL checks |
| 12 React dashboard | Frontend Eng | [x] 6cf6abb | 73 tests, live smoke, 13 screenshots |
| 13 E2E + security tests | QA + Lead | [x] 57de8e8, 09470ee | 64 e2e + 18 services security tests. Review findings fixed |
| 14 Benchmarks | QA | [x] 09470ee | 3 methods × 3 scales (100/1k/10k) + feedback experiment |
| 15 Documentation | Docs agent + Lead | [x] | all brief §16 documents. Acceptance: docs/development_progress.md |

## Blocker log
| Date | Blocker | Resolution |
|---|---|---|
| 2026-11-04 | Docker not installed | **Resolved:** Docker Desktop installed by the user. Compose stack verified healthy |
| 2026-11-04 | `.claude/agents` not loaded mid-session | **Resolved:** loaded in the new session. Real agent types in use |
