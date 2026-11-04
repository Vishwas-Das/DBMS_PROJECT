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
| Stage | Owner | Status |
|---|---|---|
| 0 Inspect repo + verify reported state | Lead | [x] 288 tests, gates clean (verified independently) |
| 1 Preserve graph + crypto | Lead | [x] unchanged and passing |
| 2 DB schema, migrations, synthetic data, adapters | Database Eng | [~] agent running |
| 3 Schema + FK discovery | Discovery Eng | [~] agent running (unit). Integration pending stage 2 |
| 4 SQL lineage (SQLGlot) | Discovery Eng | [~] agent running |
| 5 Record-level resolution | Discovery Eng | [~] agent running (unit). Integration pending stage 2 |
| 6 Lineage integration + graph facade | Graph Eng | [~] agent running |
| 7 Planner + policy engine | Erasure Eng | [~] agent running (unit) |
| 8 Safe execution | Erasure Eng | [~] agent running (unit). Integration pending stage 2 |
| 9 Independent verification + metrics | Verification Eng | [~] agent running (unit) |
| 10 DB-backed audit + evidence | Verification Eng + DB Eng (PgAuditSink) + Lead | [~] |
| 11 FastAPI integration (services impl) | Lead | [ ] waiting on repositories |
| 12 React dashboard | Frontend Eng | [~] agent running against the OpenAPI contract |
| 13 End-to-end + acceptance tests | QA/DevOps + Lead | [ ] |
| 14 Benchmarks | QA/DevOps | [ ] |
| 15 Documentation + demo | Lead | [ ] |

## Blocker log
| Date | Blocker | Resolution |
|---|---|---|
| 2026-11-04 | Docker not installed | **Resolved:** Docker Desktop installed by the user. Compose stack verified healthy |
| 2026-11-04 | `.claude/agents` not loaded mid-session | **Resolved:** loaded in the new session. Real agent types in use |
