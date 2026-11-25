# Development Progress and Final Acceptance Report

> Historical report of 2026-11-05. Later changes and re-run results: [final_repository_audit.md](final_repository_audit.md).

Final state: `main`, 31 commits (4471f3d … this commit), 2026-11-05. Every status below is backed by a test or a command that was actually run on the final code. "Test evidence" names the suite or test.

## 1. Final verification run (final code)

| Gate | Result |
|---|---|
| ruff / ruff format | All checks passed / 152 files formatted |
| mypy --strict (`backend/`) | no issues in 81 source files |
| import-linter (layering) | 5 contracts kept, 0 broken |
| Unit tests (fakes, no DB) | **906 passed** |
| Integration: db + services (`lae_test_lead`) | **202 passed** |
| Integration: discovery (`lae_test_disc`) | **42 passed** |
| Integration: verification (`lae_test_verif`) | **24 passed** |
| E2E security (`lae_test_qa`) | **64 passed** |
| Frontend: typecheck / lint / Vitest / build | clean / clean / **73 passed** / built |
| **Total automated tests** | **1 311** (1 238 backend + 73 frontend) |
| Live checks on the dev DB (1 000 synthetic customers) | API health ok. 401 without token. 400 for a foreign Host. CLI LIVE erasure of customer 300 (row gone, verified by SQL). Exported evidence verifies offline. A tampered bundle is rejected. `lae audit verify` valid |

Line coverage (unit suite only): 77 % overall. lineage 100 %, audit and verification 100 %, planner 99 %, executor 100 %, discovery 97 %. `lae.db` 98 % under its integration suite. **Coverage is not correctness:** see [testing.md](testing.md) "What coverage does not prove".

## 2. Acceptance matrix (mission brief §21)

| # | Criterion | Status | Relevant files | Test evidence | Known limitations |
|---|---|---|---|---|---|
| A | Database schema works | **COMPLETE** | `backend/migrations/versions/0001–0003`, `lae/db/models/` | model ↔ migration parity, 108+ constraint cases, trigger tests (`tests/integration/db`) | Prototype roles. `lae_owner` is still a superuser for migrations |
| B | Synthetic dataset loads | **COMPLETE** | `db/fixtures/generator.py`, `app_schema.sql`, `app_derived.sql` | determinism and ground-truth tests. Dev DB 1 000 / 3 000 / 1 000 / 500 / 500. Large scale 10 000 customers in benchmarks | Synthetic, one generator |
| C | Foreign-key discovery | **COMPLETE** | `lae/discovery/foreign_keys.py`, `db/catalog_queries.py` | 12/12 FKs vs `pg_get_constraintdef`, composite order, all ON DELETE kinds | — |
| D | SQL lineage extraction | **COMPLETE** | `lae/discovery/sql_lineage.py` | 55-case matrix + real `pg_get_viewdef` (customer_ltv / customer_segment KEY_PRESERVING, daily_revenue AGGREGATE) | Recursive CTEs, UDFs and dynamic SQL become OPAQUE |
| E | Record-level dependency discovery | **COMPLETE** | `lae/discovery/records.py` | precision = recall = 1 vs ground truth (FK + registered) at tiny/small/default. Roles incl. SHARED both ways | Hidden copies without an FK need the residual scan + confirmation |
| F | NetworkX graph with real data | **COMPLETE** | `lae/lineage/` | 75 unit tests (100 %). Used by every real-DB run. Served to the UI | — |
| G | Erasure plans generated correctly | **COMPLETE** | `lae/planner/` | 135 planner tests + Hypothesis. e2e plans on real PG | Pseudonym-vs-NULL choice not hash-bound |
| H | Policy restrictions enforced | **COMPLETE** | `planner/planner.py`, `services/pipeline.py` | retained orders anonymised not deleted. RETAIN rows untouched. Benchmarks: 0 retention violations (B2: 258–331). Shared rows never destroyed | Policies are operator input, not legal advice |
| I | Approved operations execute safely | **COMPLETE** | `lae/executor/`, `services/pipeline.execute` | dry run leaves rows unchanged. Hash binding. Four-eyes. Drift gives STALE_PLAN with no changes. All-or-nothing. Idempotent. Concurrency gives exactly one commit (3/3). Runs as non-superuser `lae_app` | RECOMPUTE_TABLE has no rowcount assertion |
| J | Independent verification | **COMPLETE** | `lae/verification/` | 24 real-PG tests. Residual scan catches hidden copies FK erasure leaves. VERIFIED / PARTIAL / FAILED + coverage | Whole-value matching only. Names excluded when ambiguous |
| K | Cryptographic evidence generated | **COMPLETE** | `lae/audit/`, `services/pipeline.issue_evidence` | golden vectors. Signed bundle + Merkle leaves per request | Unencrypted signing key (prototype) |
| L | Offline evidence verification | **COMPLETE** | `audit/evidence.verify_evidence_offline`, `lae verify-evidence` | network-blocked test. Tamper detected at exact seq (hash + envelope). Live CLI check on the dev DB | Anchors are stored on the same host |
| M | FastAPI endpoints work | **COMPLETE** | `lae/api/`, `lae/services/impl.py` | 23 paths. API unit tests. 18 e2e/security API tests on real PG. 64 QA e2e tests | Single shared token. Self-asserted actor |
| N | React dashboard shows real backend data | **COMPLETE** | `frontend/` | 73 Vitest tests. Live browser smoke via Edge/CDP. 13 screenshots from the live system | Smoke test is not in CI. Playwright not installed (needs approval) |
| O | Critical automated tests pass | **COMPLETE** | `backend/tests/**`, `frontend/src/**` | §1: 1 311 passed, 0 failed | CI workflow written, **not run** here |
| P | Reproducible benchmarks | **COMPLETE** | `benchmarks/`, `benchmarks/results/` | 3 methods × 3 scales × 20 subjects + feedback experiment, seed 42, environment recorded, CSV/JSON/SVG | 20 subjects per set. Laptop timings |
| Q | Docs reflect the implementation | **COMPLETE** | `README.md`, `docs/*.md` | written against the code. Discrepancies found by the docs agent were fixed or documented | Design-time docs (architecture §, AT wording) are partly superseded by change requests |
| R | Starts locally | **COMPLETE** | `scripts/*.ps1`, `docker-compose.yml`, `.env.example` | `setup.ps1`, `start-backend.ps1`, `start-frontend.ps1` run by QA. API started from `.env` and probed | Compose `full` profile (api/web containers) not provided |

**Completion: 18 / 18 criteria COMPLETE**, each with the limitations listed. None is BLOCKED or NOT STARTED.

## 3. Acceptance tests AT-01 … AT-24 (development_plan.md §6)
The mapping is in [testing.md](testing.md). Every AT has at least one covering test. **Divergences from the original wording** (the code's behaviour is the documented one):
- **AT-06:** inference at θ = 0.95 does *not* suggest `marketing_event_log.email` (tiny scale: containment 17/19, confidence 0.805). The residual scan finds those copies, and its feedback creates the suggestion.
- **AT-11:** reworded (CR erasure-1): ANONYMIZE under a deleted parent with CASCADE gives `CASCADE_DESTROYS_RETAINED`.
- **AT-15:** a failed LIVE run is recorded as `EXECUTION_FAILED` (`EXECUTION_ROLLED_BACK` is used for dry runs).
- **AT-17:** a repeat LIVE execution returns 409 `INVALID_STATE` (the plan is no longer APPROVED) rather than `ALREADY_EXECUTED`. The DB unique index still guarantees idempotency.

## 4. What each agent did (Claude Code subagents)

| Agent | Rounds | Delivered | Notable findings |
|---|---|---|---|
| Lead Architect | throughout | architecture, contracts (M0, CR lead-1), API layer, service pipeline + impl, CLI, migrations 0002/0003, e2e + security tests, integration, all decisions, README / security / limitations / benchmarking / this report | found the hidden-copy feedback gap and the localhost/IPv6 5 s connection delay |
| Graph Engineer | 2 | `lae.lineage`: LineageGraph, RecordGraph, BFS/DFS facade, evidence serialisation, `set_role` | 100 % coverage. Clarified 8 contract ambiguities |
| Verification & Crypto Engineer | 3 | canonical JSON tests, HMAC, hash chain, Merkle, Ed25519, evidence + offline verifier, verification engine, metrics, anchors, real-PG tests | **PostgreSQL `\v` is not an escape** (LOWER_TRIM bug). Hash chain did not bind actor/time (envelope fix) |
| Database Engineer | 1 | Alembic baseline, models with parity test, `app` schema, generator + ground truth, PG adapters, audit sink, repositories | 183 integration tests. Typed key binding |
| Lineage Discovery Engineer | 2 | catalog, fingerprint/drift, FK discovery, SQLGlot lineage (55 cases), artifacts, inference, PII, record discovery with roles, residual probes | many SQLGlot 30.29 quirks documented. Ground-truth P/R = 1 |
| Erasure Engine Engineer | 2 | planner (all categories, conflicts, ordering, hashing, state), executor | Hypothesis found an impossible-order bug and a ranked-batch FK bug. Shared-record safety |
| Frontend Engineer | 2 | React/Vite/Tailwind/React Flow UI, 7 pages, CDP smoke e2e, screenshots | **namesake false positives** in the residual scan (fixed) |
| QA & DevOps Engineer | 1 | 64 e2e security tests, benchmarks + report, setup/start/check scripts, CI, test-DB script | **audit-chain fork under concurrency** (fixed). 500 on hostile keys (fixed). `.env` leaking into tests (fixed) |
| Independent security reviewer | 1 (read-only) | 3 High / 7 Medium / 8 Low findings | non-superuser role, auth on reads, SQL breakout, HKDF, stuck re-plan bug |
| Documentation agent | 1 | methodology, algorithms, mathematical functions, API (real samples), testing | found GET-creates-snapshot and 500-on-PlanError (both fixed) |

## 5. Defects found and fixed after first implementation (selected)
1. LOWER_TRIM SQL used `\v` (a literal `v` in PostgreSQL) → `\x0B`.
2. Audit hash did not cover actor/timestamp/entity → hashed envelope + column check.
3. Superseding an approved plan violated a CHECK, so requests were stuck after STALE_PLAN → migration 0002.
4. App ran as superuser and could disable the audit trigger → `lae_app` role.
5. Artifact SQL breakout via the simple-query protocol → single-SELECT validation + extended protocol.
6. Unauthenticated reads exposed subject linkage → token on all routes, TrustedHost.
7. Namesakes made correct erasures FAILED → ambiguous probes excluded (trade-off documented).
8. Concurrent LIVE executions forked the audit chain → UNIQUE(prev_hash) + execution lock (exactly one commit).
9. Raw keys survived in superseded plan versions → purged across versions.
10. `localhost` → `::1` after the localhost-only binding added 5 s per connection → `127.0.0.1`.

## 6. Remaining blockers
None blocking local use. Open items are listed in [limitations.md](limitations.md) and [security_analysis.md §4](security_analysis.md): external anchor storage, per-user authentication, physical erasure, RECOMPUTE rowcount check, and an unrun CI.
