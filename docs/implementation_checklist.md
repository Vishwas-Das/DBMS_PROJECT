# Implementation Checklist

Legend: `[x]` done and verified (test output recorded) · `[~]` in progress · `[ ]` not started · `[!]` blocked.
Rule: an item is ticked only when an automated test has passed against the real artefact. For DB items, that means a real PostgreSQL.

## Environment
- [x] Git repository initialised (`main`)
- [x] Python venv + backend packages (SQLAlchemy 2.1.8, SQLGlot 30.29.0, NetworkX 3.7, psycopg 3.3.8, cryptography 50.0.2)
- [!] WSL2 + Docker Desktop. **Blocked: needs admin + reboot (user action)**
- [ ] PostgreSQL 16 + pgcrypto reachable for integration tests
- [ ] pgvector available (deferred: not needed for the vertical slice)

## M0: Contracts
- [x] `backend/pyproject.toml` (package `lae`, pytest markers, ruff, mypy, import-linter)
- [x] `lae.errors`, `lae.config`, `lae.contracts.{enums,types,ports}`
- [x] Contract smoke tests: 8 passed. ruff clean. mypy --strict clean. import-linter 4/4 kept

## Vertical slice VS-1 (core only: FK lineage, DELETE/EXPECT_CASCADE/NULLIFY_REFERENCE)
| # | Stage | Module / owner | Status |
|---|---|---|---|
| 1 | Synthetic customer creation (deterministic generator + ground truth) | db/fixtures · DB Eng | [ ] |
| 2 | PostgreSQL storage (`lae` Alembic baseline, `app` slice schema) | db · DB Eng | [ ] |
| 3 | Foreign-key discovery (catalog + FKs from pg_catalog) | discovery · Discovery Eng | [ ] |
| 4 | Record-level dependency identification | discovery · Discovery Eng | [ ] |
| 5 | NetworkX lineage graph + RecordGraph | lineage · Graph Eng | [x] 49 tests, 100 % coverage, verified by Lead |
| 6 | Erasure plan generation (+ hash, conflicts subset) | planner · Erasure Eng | [ ] |
| 7 | Dry-run execution (rollback, counts) | executor · Erasure Eng | [ ] |
| 8 | Approved erasure (hash-bound, drift check, rowcount assertions) | executor + services · Erasure Eng / Lead | [ ] |
| 9 | Verification (ROW_ABSENT, CASCADE_COMPLETE, REFERENCE_NULLIFIED, REDISCOVERY_EMPTY) checked against the DB | verification · Crypto Eng | [ ] |
| 10 | Audit evidence (canonical JSON, hash chain, Merkle, Ed25519, offline verify) | audit · Crypto Eng | [~] canonical JSON done (Lead). Primitives: agent running |
| 11 | Service-layer integration + CLI `lae erase` end-to-end | services/cli · Lead | [ ] |
| 12 | E2E test: actual DB row counts before/after equal ground truth | tests/e2e · QA | [ ] |

## Deferred until after VS-1
- [ ] SQLGlot SQL lineage + derived artifacts (matviews, CTAS, embeddings)
- [ ] Inferred relationships, PII suggestions, residual HMAC scan
- [ ] ANONYMIZE policies + full conflict catalogue
- [ ] FastAPI routers (after service contracts are stable)
- [ ] Frontend (after API is stable)
- [ ] Docker Compose full stack verified running
- [ ] Benchmarks B1–B6
- [ ] Acceptance tests AT-01…AT-24

## Blocker log
| Date | Blocker | Impact | Owner | Resolution |
|---|---|---|---|---|
| 2026-11-04 | WSL2/Docker Desktop not installed. Shell not elevated | Can't run `docker compose`. Integration tests need some PostgreSQL | User | pending |
| 2026-11-04 | Re-checked after the user said Docker was installed: **no Docker found** (no docker.exe, no registry entry, not on PATH). `wsl --status`: Virtual Machine Platform component not enabled | DB stages 1–4, 7–9 blocked | User | pending: needs admin `wsl --install --no-distribution`, reboot, Docker Desktop |
| 2026-11-04 | `.claude/agents/*` not loaded as agent types (created mid-session) | Specialists run as general-purpose agents that load their role file | Lead | workaround in place. Loads natively next session |
