# Final Repository Audit

Date: 2026-11-25. Code audited: `f8878c8`, plus the fixes listed in section 7 (`b36cd1d` … `b985066`). All results below come from commands run during this audit. LAE is an academic research prototype. Nothing here is a claim of legal compliance or of physical destruction of data.

**Verdict: published as a research prototype**, with the open findings in section 8 disclosed. No CRITICAL finding is open. One HIGH finding (E2) is open and documented.

## 1. Repository health

- Branch `main`. It contains the published head `42d0bde`, so it fast-forwards without rewriting anything. `9b00d54` and `73534a2` are present, and the 34 original commits are unchanged.
- Remote `origin`: a GitHub repository (public during the audit, now private) whose `main` was at `42d0bde` (9 commits) before this publication.
- No tracked file matches an ignore rule. Packed size is about 4.2 MiB, and the largest published file is a 377 kB screenshot.

## 2. Functional validation (clean clone, Windows 11, Windows PowerShell 5.1)

A fresh clone was set up with `scripts\setup.ps1` against a new Docker volume in a separate Compose project. The development volume was never mounted.

| Check | Result |
|---|---|
| `docker compose config` | valid |
| `setup.ps1` (venv, `npm ci`, `.env`, DB, migrations 0001–0003, seed, key) | exit 0, after fixes F3 and F4 (the final run reused the venv and `.env` made by the earlier attempt and started with a new empty volume) |
| API and UI start scripts | `/api/health` ok directly and through the Vite proxy; UI 200; `/api/stats` 401 without token, 200 with token |
| Audit chain, export, offline evidence check | valid (11 events); signature, Merkle root and chain ok; exit 0 |
| CLI LIVE erasure of customer 120 | COMMITTED, outcome FAILED, coverage 9/10. Correct: 4 residual matches in `marketing_event_log` (no foreign key) were reported |
| Re-seed guard | refused, exit 4 |

## 3. Test results

| Gate (`scripts\check.ps1`, clean clone) | Result |
|---|---|
| ruff check and format (backend, benchmarks, scripts) | pass |
| mypy strict | no issues in 81 files |
| import boundaries | 5 kept, 0 broken |
| Unit | 907 passed |
| Integration (real PostgreSQL): db / discovery / services / verification | 184 / 42 / 20 / 24 passed |
| End-to-end security | 64 passed |
| Frontend typecheck, lint, Vitest, build | pass, pass, 73 passed, built |
| **Total** | **1 241 backend + 73 frontend = 1 314 passed, 0 failed** |
| `npm audit` | 0 vulnerabilities |

**GitHub Actions.** The first run (45634175782, commit `745a478`, 2026-11-25) **failed**. Frontend job: passed. Backend job: install and ruff passed, mypy failed, and import rules plus all test steps were skipped as a result. Cause: `mypy backend/src/lae` ran from the repository root, where `[tool.mypy]` in `backend/pyproject.toml` does not apply (3 errors, reproduced on Windows and Linux; none from `backend/`). The workflow now runs mypy from `backend/` with the same strict settings. Database URLs and the health check also use 127.0.0.1, and the pip cache key includes `constraints.txt`.

Before that fix was pushed, every backend CI command was run on Linux (`python:3.12-slim` with `pgvector/pgvector:pg16`): all exit 0, 907 unit + 270 integration + 64 end-to-end tests passed, none skipped. This reproduces the commands, not the GitHub runner. The result of the GitHub run that follows the fix is **not recorded here**, because it happens after this text is committed: see the Actions page.

Not run: the benchmarks (too long; the corrected claims were checked against the stored raw data and the tables against `summary.md`), the Edge/CDP browser smoke test, `setup.sh`, and a Python dependency vulnerability scan.

## 4. Security findings

Status: FIXED, OPEN (documented in [limitations.md](limitations.md) or [security_analysis.md](security_analysis.md)), or OWNER (needs a decision by the owner). Findings come from tracing the code. "Reproduced" marks those also demonstrated by a failing test or command.

| Id | Severity | Finding and evidence | Status |
|---|---|---|---|
| E1 | HIGH | `lineage_edge.id` was global but discovery ids are schema-stable, so any schema change after the first snapshot failed with a `pk_lineage_edge` violation and blocked new requests. Reproduced | FIXED |
| E2 | HIGH | The signed bundle (`audit/evidence.py`) has no VERIFIED / PARTIAL / FAILED outcome: a PARTIAL erasure is signed with status `PASSED`. The docs wrongly told auditors to read `verification.outcome` | Docs FIXED. Format OPEN: adding it changes a frozen contract and golden vectors |
| E3 | HIGH | `check.ps1` reported **0** for mypy and lint-imports although Windows had blocked both programs (stale exit code). Reproduced | FIXED (fails closed, uses `python -m`) |
| E4 | HIGH (privacy) | A staged PDF for an unrelated project contained third-party names and student ids | FIXED (unstaged, `*.pdf` ignored, never in `main`). See section 6 |
| E5 | MEDIUM | A plan superseded between the pre-checks and the SERIALIZABLE transaction was still executed. Reproduced | FIXED (re-checked inside the transaction) |
| E6 | MEDIUM | Re-verifying a closed, purged request recorded an ERROR run that displayed as FAILED | FIXED for purged requests (refused before a run is created). An unexpected error can still leave a run RUNNING: OPEN |
| E7 | MEDIUM | `lae db seed` and `setup.ps1` silently restored erased subjects | FIXED (refuses when requests exist) |
| E8 | MEDIUM | Re-verification never re-issues evidence or purges keys | OPEN |
| E9 | MEDIUM | One operator can satisfy four-eyes: identity is self-asserted and the CLI supplies both names. Single shared token | OPEN (prototype scope) |
| E10 | LOW | `setup.ps1` printed database URLs in error text; `setup.sh` seeded as the restricted role; the default database URL was the superuser; CI published PostgreSQL on all interfaces | FIXED |
| E11 | LOW | UI `signature_valid` trusts the key in the database; anchors are not checked by any command; unused `lae_executor` login; raised discovery limits cannot execute; duplicate-request race; tracebacks from unexpected DB errors could include row detail; URL passwords visible in process arguments | OPEN |
| E12 | LOW | Development passwords (`lae_owner_dev`, …) are committed as localhost defaults; the port binds to 127.0.0.1 | OPEN by design |
| E13 | INFORMATIONAL | `/docs`, `/redoc`, `/openapi.json` need no token; CI actions use version tags, not commit hashes | OPEN |
| E14 | INFORMATIONAL | `backend/tests/unit/audit/vectors/test_signing_key.pem` is a deliberate test-only key used by golden vectors. It signs nothing real. A secret scanner may flag it | Accepted |
| E15 | INFORMATIONAL | Every commit carries the author e-mail address, including the 9 already public | OWNER |

**Secrets in history.** The actual `LAE_HMAC_KEY`, `LAE_API_TOKEN` and signing-key values were searched for in every commit of every ref: **not found**. Patterns for cloud, GitHub and Slack tokens, JWTs and private keys matched only E14. No rotation or history rewrite is needed. (A dedicated scanner such as gitleaks was not installed.)

**Confirmed controls:** token on every `/api` router except health, bytes comparison, unset token refuses; identifiers validated and quoted with bound values; hash re-checked at approval and execution; executor never commits; append-only audit triggers with a non-owner application role; HKDF subkeys; benchmarks and test fixtures refuse the `lae` database.

## 5. Documentation review

Corrected: benchmark narrative (remaining hidden copies belong to blocked plans; 83–95 %; 192.8 s outlier; slowdown ratios), API error mapping, snapshot behaviour of `GET /api/lineage/schema`, test inventory, "benchmarks not committed" statements, containment versus confidence, roles and migrations, API prefix, document status, and a non-existent `lae bench` command in the UI. Result tables match `benchmarks/results/summary.md`. Relative links resolve. No document claims Kafka, Redis, Debezium or erasure in external systems as implemented, legal compliance, or physical destruction. The 13 screenshots match the UI and show synthetic data; they predate the namesake fix, and the README says so. Not every sample in `api_documentation.md` was replayed, and the walkthrough was sampled rather than re-verified line by line.

## 6. Git hygiene

Uncommitted work found at the start: the PDF (E4) and a stray letter `a` in `technical_walkthrough.md` (reverted). Local refs `refs/agents/*` (editor checkpoints) contain that PDF. They are not pushed by `git push origin main`, but **`git push --mirror` must not be used**.

## 7. Issues fixed

| Id | Fix | Commit |
|---|---|---|
| F1 | Snapshot edge ids (E1), LIVE re-check (E5), purged re-verification (E6), 2 regression tests | `b36cd1d` |
| F2 | Fail-closed `check.ps1` (E3), seed guard (E7), E10 items, `.env.example` four-eyes flag, UI benchmark command | `8aa2e9a` |
| F3 | `backend/constraints.txt`: exact tested versions. With unpinned dependencies a fresh install resolved new builds that Windows Application Control blocked | `8aa2e9a` |
| F4 | Healthcheck over TCP: on a fresh volume, setup connected before PostgreSQL was ready. Reproduced | `b985066` |
| F5 | `.gitignore` additions | `a267179` |
| F6 | Documentation corrections | `a0df038`, `b985066` and the commit adding this report |

`.dockerignore` is not needed: no image is built (Compose uses the stock pgvector image). E6 and E7 have no dedicated automated test; E7 was checked by hand (section 2).

## 8. Remaining known limitations

E2 (bundle format), E6 (partly), E8, E9 and E11–E13 above, plus [limitations.md](limitations.md) and [security_analysis.md](security_analysis.md). No license file exists. Windows Application Control may block the unsigned `lae.exe` launcher; `python -m lae.cli` works.

## 9. Files excluded from publication

`.env`, `secrets/`, `.venv/`, `node_modules/`, `frontend/dist/`, caches, `var/`, `evidence_out/`, `db/fixtures/out/`, `benchmarks/results/raw/`, `.vscode/`, and all PDFs (including the unrelated course document).

## 10. Publication readiness

Published on 2026-11-25 by normal fast-forward push of `main` (`42d0bde` to `745a478`). The repository was public at that time and was made private afterwards by the owner. CI status: section 3.
