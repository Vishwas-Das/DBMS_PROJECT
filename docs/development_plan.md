# Development Plan

Status: **DRAFT v0.1, awaiting approval**. Nothing in this plan runs until the Lead Architect receives explicit approval.

---

## 1. Prerequisites (approval required for each)

| # | Action | Why | Status |
|---|---|---|---|
| P1 | CPU virtualization | Needed by Docker Desktop and WSL2 | **already enabled** (corrected 2026-11-04) |
| P2 | Install WSL2 (`wsl --install`, admin + reboot) + Docker Desktop | `docker compose` with `pgvector/pgvector:pg16` | **user action (admin)** |
| P2-alt | *Only if P1 is impossible:* PostgreSQL 16 (EDB installer) + Visual Studio Build Tools + pgvector built with `nmake /F Makefile.win` | Native fallback | pending |
| P3 | `git init`, `.gitignore`, initial commit of docs | Agent worktrees and branches. Traceability | pending |
| P4 | `py -3.12 -m venv .venv` + backend packages (§2) | | pending |
| P5 | `npm create vite@latest frontend -- --template react-ts` + frontend packages (§2) | | pending |
| P6 | Generate the dev signing key into `secrets/` (gitignored), `LAE_HMAC_KEY` into `.env` | | pending |

## 2. Dependencies to install (after approval)

**Backend runtime:** `fastapi`, `uvicorn[standard]`, `pydantic>=2.7`, `pydantic-settings`, `sqlalchemy>=2.0.30`, `psycopg[binary]>=3.2`, `alembic`, `sqlglot` (pinned exact version, because lineage behaviour changes across releases), `networkx>=3.3`, `pgvector`, `cryptography>=42`, `typer`.
**Backend dev:** `pytest`, `pytest-cov`, `hypothesis`, `httpx`, `ruff`, `mypy`, `import-linter`.
**Frontend:** `react`, `react-dom`, `react-router-dom`, `@tanstack/react-query`, `@xyflow/react`, `@dagrejs/dagre`. Dev: `typescript`, `vite`, `vitest`, `@testing-library/react`, `jsdom`, `openapi-typescript`, `eslint`.

Pinning: after the first successful install, `pip freeze > backend/requirements.lock` and commit `frontend/package-lock.json`.

---

## 3. Milestones

| M | Name | Owner(s) | Depends on | Exit criteria |
|---|---|---|---|---|
| **M0** | Bootstrap & frozen contracts | Lead | P1–P6 approved | Git repo. `backend/pyproject.toml`. `lae.contracts`, `lae.errors`, `lae.config` implemented exactly as in module_contracts.md. Pydantic API schemas. `docs/openapi.json` exported. `import-linter` config. Docs at v1.0 |
| **M1** | Data layer + infrastructure (parallel) | DB Eng · QA/DevOps · Graph Eng · Crypto Eng · Frontend Eng | M0 | **DB:** Alembic `0001_baseline` (all `lae` tables, checks, triggers, roles), `app` fixtures, generator + ground truth, `PgCatalogReader`/`PgTargetReader`/`PgTargetWriter`/`PgAuditSink`, repositories. **QA:** `docker-compose.yml`, `db/init`, pytest config + markers, fakes for ports, CI script. **Graph:** `LineageGraph`, `RecordGraph` (pure, fully unit-tested). **Crypto:** `canonical`, `hmac_keys`, `hashchain`, `merkle`, `signing` + golden vectors. **Frontend:** app shell, routing, generated API types, `graphAdapter` |
| **M2** | Discovery | Discovery Eng | M1 (DB, Graph) | Catalog + FK (composite, all `ON DELETE` kinds), SQLGlot lineage with a classification test matrix, inference, PII suggestions, record discovery against fixtures. Precision/recall = 1.0 on declared relationships of the demo |
| **M3** | Planning & execution | Erasure Eng | M2 | Planner (policies, roles, cascades, conflicts, ordering, hashing). Executor (drift check, locking, rowcount assertions, DRY_RUN). All demo hard cases produce the expected plan or conflict |
| **M4** | Verification & evidence | Crypto Eng | M3 | All 8 check types. Residual scan finds `marketing_event_log` copies. Evidence bundle + offline verifier CLI. Purge |
| **M5** | Integration | Lead (+ Frontend Eng for UI wiring) | M4 | Services + routers. End-to-end workflow through API and UI. Audit events in the same transaction as state changes |
| **M6** | Evaluation | QA/DevOps (+ Lead review) | M5 | Benchmarks B1–B6 with results JSON + `summary.md`. Acceptance tests AT-01…AT-24 green in CI. Docs updated with measured results and limitations |

Parallel waves: **Wave A** = M1 (five agents, disjoint directories). **Wave B** = M2 → M3 (sequential: the planner needs real `RecordDiscoveryResult`s. The Erasure Eng may start on the planner using `RecordGraph` fakes while M2 finishes). **Wave C** = M4 ‖ frontend pages ‖ benchmark harness.

Integration gates: at the end of every milestone the Lead runs the full test suite, reviews the diff against the contracts and merges. Agents never merge.

---

## 4. Agent responsibility matrix

R = responsible (writes the code), A = accountable (approves/merges), C = consulted, I = informed.

| Area / path | Lead | DB | Discovery | Graph | Erasure | Verif+Crypto | Frontend | QA/DevOps |
|---|---|---|---|---|---|---|---|---|
| `docs/**`, `CLAUDE.md`, `.claude/agents/**` | **R/A** | C | C | C | C | C | C | C |
| `backend/pyproject.toml`, `backend/src/lae/{contracts,errors.py,config.py}` | **R/A** | C | C | C | C | C | I | C |
| `backend/src/lae/db/**`, `backend/migrations/**`, `backend/alembic.ini`, `db/fixtures/**` | A | **R** | C | I | C | C | I | C |
| `backend/src/lae/discovery/**` | A | C | **R** | C | I | I | I | C |
| `backend/src/lae/lineage/**` | A | I | C | **R** | C | I | I | C |
| `backend/src/lae/planner/**`, `backend/src/lae/executor/**` | A | C | I | C | **R** | C | I | C |
| `backend/src/lae/verification/**`, `backend/src/lae/audit/**`, `lae verify-evidence` CLI cmd | A | C | I | I | C | **R** | I | C |
| `backend/src/lae/services/**`, `backend/src/lae/api/**`, `backend/src/lae/cli.py` | **R/A** | C | C | C | C | C | C | C |
| `frontend/**` | A | I | I | I | I | I | **R** | C |
| `docker-compose.yml`, `db/init/**`, `Dockerfile*`, `scripts/**`, `.github/**`, `backend/tests/conftest.py`, `backend/tests/fakes/**`, `backend/tests/e2e/**`, `benchmarks/**` | A | C | I | I | I | I | C | **R** |
| Unit + integration tests *for own module* (`backend/tests/{unit,integration}/<module>/**`) | A | R (db) | R (discovery) | R (lineage) | R (planner, executor) | R (verification, audit) | R (frontend `*.test.tsx`) | C |

**File-collision rule:** a path has exactly one R. An agent that needs a change outside its paths (for example, a new port method) writes a **change request** to `docs/change_requests/<agent>-<n>.md`, and the Lead applies it to the contracts first.

---

## 5. Agent working protocol

1. The Lead launches each specialist as a Claude Code subagent (`.claude/agents/<name>.md`) with `isolation: worktree` on branch `agent/<name>/<milestone>`.
2. Each brief contains: milestone, owned paths, contract sections to implement, acceptance tests to make green, and the explicit "do not touch" list.
3. The agent finishes by reporting: files changed, tests run (command + output summary), deviations or change requests, open risks.
4. The Lead reviews, runs `pytest` + `ruff` + `mypy` + `lint-imports`, and merges to `main`.

---

## 6. Proposed acceptance tests

Marker legend: `unit` (no DB), `integration` (real PostgreSQL), `e2e` (API end-to-end), `bench`.

| ID | Level | Scenario | Pass condition |
|---|---|---|---|
| AT-01 | integration | Snapshot of demo `app` | All 15 relations found with correct kinds and PKs. Fingerprint is stable across two scans. It changes after `ALTER TABLE … ADD COLUMN` |
| AT-02 | integration | FK discovery | Every declared FK found. `shipment → order_line` has ordered columns `(order_id,line_no)`. `on_delete_action` correct for c/n/r/a |
| AT-03 | unit | SQLGlot: `customer_ltv` | `traceability=KEY_PRESERVING` via `customer_id`. `total_spent` is `AGGREGATE` from `payment.amount` |
| AT-04 | unit | SQLGlot: `daily_revenue` | `traceability=AGGREGATE`. No key mapping |
| AT-05 | unit | SQLGlot: unparseable SQL / UDF | `OPAQUE` + warning. No exception |
| AT-06 | integration | Inference | `marketing_event_log.email → customer.email` suggested with confidence ≥ 0.95. **Not traversed** until confirmed |
| AT-07 | integration | Record discovery, customer 42 | Equals ground truth for all FK- and key-preserving-reachable rows. `product` rows **not** included |
| AT-08 | integration | Other subjects | Customer 77 (referred by 42) has role `REFERENCING_OTHER_SUBJECT`. The plan has `NULLIFY_REFERENCE` on `customer.referrer_id`. Customer 77 survives execution |
| AT-09 | integration | Shared record | A `ticket_message` authored by another customer on 42's ticket produces `SHARED_RECORD` (BLOCKING) |
| AT-10 | unit | Ordering | For every plan, every DELETE of a child precedes the DELETE of its parent. Derived steps come after all base steps, in `artifact_order` |
| AT-11 | integration | Retention conflict | `purchase_order` = ANONYMIZE + `order_line` CASCADE produces a `CASCADE_DESTROYS_RETAINED` conflict, and the plan is `BLOCKED` |
| AT-12 | unit | Plan hash | Same inputs give an identical hash across runs and processes. Any item change gives a different hash |
| AT-13 | e2e | Approval binding | Approve with the wrong hash gives 409 `PLAN_HASH_MISMATCH`. Execute an unapproved plan gives 409 `INVALID_STATE` |
| AT-14 | integration | Dry run | Correct per-step counts. Data byte-identical afterwards (table checksums equal) |
| AT-15 | integration | Atomicity | Injected failure at step k gives full rollback (all tables unchanged) and a `EXECUTION_ROLLED_BACK` event |
| AT-16 | integration | Drift | A new `purchase_order` for 42 inserted after approval gives `STALE_PLAN`. Nothing changes |
| AT-17 | e2e | Idempotency | A second LIVE execution of the same plan gives 409 `ALREADY_EXECUTED` |
| AT-18 | integration | Verification pass | After the happy-path erasure, all checks `PASSED`. `DERIVED_CONSISTENT` passes for `customer_ltv`, `daily_revenue`, `customer_segment` |
| AT-19 | integration | Residual scan | Without confirming the inferred edge, the scan reports `marketing_event_log.email` hits and status `FAILED`. After confirming and re-planning, `PASSED` |
| AT-20 | integration | Embeddings | `document_embedding` rows for 42's tickets deleted. `EMBEDDING_ABSENT` passes |
| AT-21 | integration | Audit append-only | `UPDATE`/`DELETE`/`TRUNCATE` on `audit_event` raise. `verify_chain` is valid. A manual tamper (trigger disabled as superuser in the test) is detected at the exact `seq` |
| AT-22 | unit | Merkle + signature | Golden vectors match. Every inclusion proof verifies. A flipped bit fails |
| AT-23 | e2e | Offline evidence | `lae verify-evidence` succeeds with no DB running. A modified bundle fails |
| AT-24 | integration | No PII leakage | After purge: a residual scan of the **`lae` schema** for 42's identifier HMAC inputs finds 0 raw values. API responses and logs of the whole run contain no raw email/phone/subject key (log capture asserted) |

Coverage targets: ≥ 90 % lines for `planner`, `executor`, `audit`, `lineage`. ≥ 80 % overall.

---

## 7. Definition of done (every task)

- Code matches module_contracts.md signatures. `mypy --strict` passes on the owned package.
- `ruff check` + `ruff format --check` clean. `lint-imports` passes.
- New behaviour has tests. Integration tests use real PostgreSQL (no mocking of SQL semantics).
- No raw personal data in logs, exceptions, audit payloads or test snapshots.
- The doc is updated if behaviour or limits differ from the design.
