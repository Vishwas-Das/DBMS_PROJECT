# CLAUDE.md: Lineage-Aware Erasure Engine (LAE)

## Project context
An academic research prototype that, for a data subject, **discovers** personal-data dependencies across PostgreSQL tables and registered derived artifacts, builds a **lineage graph** (table / column / record level), generates a reviewable **erasure plan**, **executes** an approved plan atomically, **verifies** the result independently, and issues **signed, hash-chained audit evidence** that contains no personal data.
Goal: reliable, integrated, reproducible, and measured. Not visual polish.

Authoritative design docs (read before changing anything):
- `docs/architecture.md`: system design and algorithms
- `docs/database_schema.md`: DDL contract, naming, models, API schemas
- `docs/module_contracts.md`: **frozen** interfaces, ports, exceptions, endpoints
- `docs/development_plan.md`: milestones, ownership matrix, acceptance tests
- `docs/risks_and_limitations.md`: what is *not* guaranteed

Current phase: **architecture APPROVED (2026-11-04). Implementation in progress** (see `docs/implementation_checklist.md`). Contract extensions: `docs/change_requests/lead-1.md`.

Docker Desktop is installed but `docker` may not be on PATH in agent shells. Use `"/c/Program Files/Docker/Docker/resources/bin/docker"` (bash) or add that folder to PATH.

## Stack
Python 3.12 · FastAPI · SQLAlchemy 2.0 (sync) + psycopg 3 · Alembic · PostgreSQL 16 + pgvector + pgcrypto · SQLGlot (pinned) · NetworkX · cryptography (Ed25519) · Typer CLI · React + Vite + TypeScript · TanStack Query · @xyflow/react · pytest / Vitest · Docker Compose.

## Architecture rules
1. **Layering:** `api → services → {discovery, lineage, planner, executor, verification, audit} → contracts`. `db` is used only by `services`. Domain modules never import `lae.db`, SQLAlchemy ORM, FastAPI or `lae.services`, and get DB access only through ports in `lae.contracts.ports`. `lint-imports` enforces this.
2. **Contracts are frozen.** Change `docs/module_contracts.md` / `docs/database_schema.md` first (Lead approval), then code. Specialists file `docs/change_requests/<agent>-<n>.md` instead of editing contracts.
3. **Single owner per path** (see development_plan.md §4). Do not edit files you don't own.
4. **Lineage levels are distinct.** A table or column edge never implies affected records. Records come only from executing queries. `AGGREGATE` gives a refresh or recompute, never a row delete. `OPAQUE` gives `MANUAL_REVIEW` / `BLOCKED`.
5. **Traverse downstream only**, and only over `CONFIRMED` edges. `INFERRED` edges stay `SUGGESTED` until a human confirms them. Never delete rows in the subject-root table other than the subject.
6. **The service layer owns transactions.** The executor never commits. Audit events are appended in the **same transaction** as the state change they record.
7. **Execution:** only `APPROVED` plans whose hash equals the approval hash. SERIALIZABLE. Drift re-check inside the transaction. Assert the rowcount of every step. Any mismatch → full rollback.
8. **Verification failures are data** (`passed=False`), not exceptions.
9. **Determinism:** everything that is hashed is canonical JSON (`lae.audit.canonical`). No floats. Sorted keys and collections. UTC timestamps.

## Coding conventions
- Python: `ruff` (lint + format, line length 100), `mypy --strict` on `src/lae`. Type hints everywhere. `from __future__ import annotations`.
- Contracts: `@dataclass(frozen=True, slots=True)`, tuples not lists. Enums are `StrEnum` with values identical to the DB `CHECK` lists.
- DB naming: singular snake_case tables, `id uuid` PKs, `<table>_id` FKs, `*_at timestamptz`, `*_hash char(64)` hex, `text + CHECK` for enums, SQLAlchemy naming convention from database_schema.md §1.
- API: `/api`, snake_case JSON, Pydantic v2 with `extra="forbid"`, errors always use `{"error": {code, message, details}}`.
- Frontend: TypeScript strict. API types are generated (`npm run gen:api`) and never hand-written. No business logic in components.
- Small modules. Pure functions where possible. No global mutable state. Use `logging`, never `print`, in library code.

## Testing requirements
- `pytest` markers: `unit` (no DB), `integration` (real PostgreSQL), `e2e` (API), `bench`. Integration tests use a real database. **Don't mock SQL semantics.**
- Every change adds or updates tests. Acceptance tests AT-01…AT-24 (development_plan.md §6) are the release gate.
- Coverage ≥ 90 % for planner, executor, audit, lineage. ≥ 80 % overall.
- Golden vectors in `backend/tests/unit/audit/vectors/` must never change without a documented reason.
- Before reporting done, run `ruff check`, `ruff format --check`, `mypy`, `lint-imports` and the relevant pytest markers, and report the actual output.

## Security restrictions
- **No f-string or concatenated SQL.** Identifiers come from the catalog snapshot, are validated, and are quoted (`psycopg.sql.Identifier` / SQLAlchemy quoting). Values are always bound parameters.
- **Never log, return, raise or store raw personal data** (subject keys, emails, phones, record key values) outside the target `app` schema and the transient `subject_key` / `record_keys` columns, which are purged after evidence. Use HMACs.
- Secrets (`LAE_HMAC_KEY`, `LAE_SIGNING_KEY_PATH`, DB passwords) live in `.env` / `secrets/`, which are gitignored. Never commit, print or echo them.
- `audit_event` is append-only. Never add code that updates or deletes it.
- Don't run destructive SQL against any database other than the local dev/test DB. Don't `DROP`/`TRUNCATE` outside test fixtures.
- Don't install packages, change system settings, or delete or overwrite files without user approval.

## Development commands (Windows PowerShell)
```powershell
# one-time (after approval)
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".\backend[dev]" -c .\backend\constraints.txt
cd frontend; npm install; cd ..

# database
docker compose up -d db                       # pgvector/pgvector:pg16 on localhost:5433
lae db upgrade                                # uses LAE_ADMIN_URL (migrations need the owner role)
lae db seed --scale default --seed 42         # app schema + ground_truth.json + lae_app grants
lae keys generate --out secrets\signing_key.pem

# run
uvicorn lae.api.main:app --port 8000          # reads .env; runs as the non-superuser lae_app
cd frontend; npm run dev                      # http://localhost:5173

# quality gates (from backend/)
ruff check src tests; ruff format --check src tests
mypy src\lae
lint-imports
pytest tests -m unit
$env:LAE_TEST_DATABASE_URL="postgresql+psycopg://lae_owner:lae_owner_dev@127.0.0.1:5433/lae_test_lead"; pytest tests\integration\db tests\integration\services
cd frontend; npm run typecheck; npm run lint; npm test -- --run; npm run build

# workflow / evaluation
lae erase --key 42                            # dry run only; add --live --yes to erase
lae audit verify
lae audit export <request-id>                 # bundle.json, leaves.json, chain.json
lae verify-evidence evidence_out\bundle.json --public-key secrets\signing_key.pub.pem --leaves evidence_out\leaves.json --chain evidence_out\chain.json
python -m benchmarks.run --help               # see docs/benchmarking.md
```
Environment variables (see `.env.example`): `LAE_DATABASE_URL` (lae_app), `LAE_ADMIN_URL` (lae_owner: migrations/seed), `LAE_READER_URL`, `LAE_TARGET_SCHEMAS=app`, `LAE_HMAC_KEY`, `LAE_SIGNING_KEY_PATH`, `LAE_API_TOKEN` (≥ 24 chars), `LAE_REQUIRE_FOUR_EYES`.
Integration tests: each workstream uses its own DB `lae_test_<suffix>` via `LAE_TEST_DATABASE_URL`, because the fixtures drop schemas.
