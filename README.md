# Lineage-Aware Erasure Engine (LAE)

An academic **research prototype** (DBMS course project) that, for one data subject, discovers where their personal data lives in a PostgreSQL database, plans an explainable, policy-aware erasure, executes it safely after explicit approval, **verifies the result independently**, and issues **signed, hash-chained evidence** that contains no personal data.

It demonstrates, with measured results, why `DELETE FROM customer WHERE id = X` is not enough: in our benchmark, a hand-written deletion script failed on a foreign-key `RESTRICT` for 59 of 60 subjects. FK-only discovery deleted legally retained rows and other customers' messages, and neither baseline touched derived tables, caches, embeddings or copies without foreign keys. See [docs/benchmarking.md](docs/benchmarking.md).

> **Not a legal-compliance tool.** It erases *logical* copies in one PostgreSQL database. It does not cover backups, WAL, replicas, files or external systems. A valid signature proves the evidence is authentic, not that every copy is gone. See [docs/limitations.md](docs/limitations.md) and [docs/security_analysis.md](docs/security_analysis.md).

## What it does

```mermaid
flowchart LR
    R[Erasure request] --> D[Discovery<br/>catalog + FKs + SQLGlot lineage<br/>+ registered artifacts]
    D --> G[NetworkX lineage graph<br/>table / column / record level]
    G --> P[Policy-aware plan<br/>DELETE / INVALIDATE / RECOMPUTE / RETAIN<br/>SCHEDULE / MANUAL_REVIEW / UNRESOLVED]
    P --> A[Approve exact plan hash<br/>four-eyes]
    A --> X[Execute in one SERIALIZABLE txn<br/>drift check, locks, rowcount asserts<br/>or DRY RUN]
    X --> V[Independent verification<br/>per-item checks + residual HMAC scan<br/>VERIFIED / PARTIAL / FAILED + coverage]
    V --> E[Evidence: Ed25519-signed bundle,<br/>Merkle leaves, hash-chained audit,<br/>offline verifier]
    V -. residual hits .-> S[SUGGESTED lineage edges<br/>human confirms]
    S -.-> D
```

Stack: Python 3.12 · FastAPI · SQLAlchemy 2 + psycopg 3 · Alembic · PostgreSQL 16 + pgvector + pgcrypto · SQLGlot · NetworkX · cryptography (Ed25519, HKDF) · React + Vite + TypeScript + Tailwind + React Flow · pytest / Vitest · Docker Compose.

## Repository layout

| Path | Contents |
|---|---|
| `backend/src/lae/` | `contracts` (frozen types/ports), `discovery`, `lineage`, `planner`, `executor`, `verification`, `audit`, `db` (models, adapters, repositories), `services` (workflow), `api` (FastAPI), `cli.py` |
| `backend/migrations/` | Alembic: `0001_baseline`, `0002_security_hardening`, `0003_audit_chain_linear` |
| `db/fixtures/` | synthetic `app` schema, matviews, deterministic generator + ground truth |
| `frontend/` | React dashboard (7 pages), Vitest tests, Edge/CDP smoke e2e |
| `benchmarks/` | runner, baselines, measurement, report, `results/` (summary + SVG charts) |
| `scripts/` | `setup.ps1`, `start-backend.ps1`, `start-frontend.ps1`, `check.ps1`, `create_test_dbs.py` |
| `docs/` | architecture, schema, contracts, methodology, algorithms, mathematical functions, API, testing, benchmarking, security analysis, limitations, development progress, change requests, screenshots |

## Run it locally (Windows PowerShell)

Prerequisites: Python 3.12 (`py -3.12`), Node 22 or newer + npm, Docker Desktop.

```powershell
# 1. one-time setup: venv + packages, DB container, migrations, synthetic data, keys, .env
.\scripts\setup.ps1
#    (manual equivalent)
py -3.12 -m venv .venv
.\.venv\Scripts\pip install -e ".\backend[dev]" -c .\backend\constraints.txt   # tested versions
cd frontend; npm ci; cd ..
docker compose up -d db                    # PostgreSQL 16 + pgvector on 127.0.0.1:5433
copy .env.example .env                     # then fill LAE_HMAC_KEY (lae keys hmac) and LAE_API_TOKEN (>= 24 chars)
.\.venv\Scripts\lae db upgrade             # migrations (uses LAE_ADMIN_URL)
.\.venv\Scripts\lae db seed --scale default --seed 42   # 1 000 synthetic customers + ground truth
.\.venv\Scripts\lae keys generate --out secrets\signing_key.pem

# 2. start
.\scripts\start-backend.ps1               # API on http://127.0.0.1:8000 (docs: /docs)
.\scripts\start-frontend.ps1              # UI  on http://localhost:5173
#    open Settings in the UI, paste LAE_API_TOKEN, set different approver / executor names

# 3. tests and quality gates
.\scripts\check.ps1                       # ruff, mypy, import rules, unit, integration (own test DBs), e2e, frontend

# 4. benchmarks (drops and re-seeds lae_test_bench; never point it at `lae`)
.\.venv\Scripts\python -m benchmarks.run --scales small,default,large --subjects 20 --seed 42 `
    --db-url postgresql+psycopg://lae_owner:lae_owner_dev@127.0.0.1:5433/lae_test_bench
```

Re-seeding is refused once erasure requests exist (`lae db seed --yes` overrides it). If Windows Application Control blocks the unsigned launcher `lae.exe`, use `.\.venv\Scripts\python -m lae.cli` instead of `.\.venv\Scripts\lae`.

CLI without the UI:
```powershell
.\.venv\Scripts\lae erase --key 120                    # discovery + plan + dry run (no changes)
.\.venv\Scripts\lae erase --key 120 --live --yes       # approve + execute + verify + evidence
.\.venv\Scripts\lae audit verify                       # recompute the whole hash chain
.\.venv\Scripts\lae audit export <request-id>          # bundle.json, leaves.json, chain.json
.\.venv\Scripts\lae verify-evidence evidence_out\bundle.json --public-key secrets\signing_key.pub.pem `
    --leaves evidence_out\leaves.json --chain evidence_out\chain.json   # offline, no DB
```
(`erase --live` approves as `--approver` (default `cli-approver`) and executes as `--actor` (default `cli-operator`). The four-eyes rule rejects identical names.)

## Demonstration scenario (about 10 minutes, all data from the live system)

1. **Dashboard:** live counts (1 000 synthetic customers, 17 relations, discovered edges, audit events), and health showing DB / pgvector / audit chain status.
2. **Erasure Requests:** search a customer (emails are masked) and create a request.
3. **Erasure Plan:** run discovery, then generate the plan.
   - Show the categories: DELETE the customer and addresses, RETAIN + anonymise the tax-relevant orders (legal basis shown), RECOMPUTE `customer_ltv` / `daily_revenue` / `customer_segment`, INVALIDATE the cache, SCHEDULE the external CRM export.
   - Each row has a PII-free reason and the edges behind it.
   - Point out the full plan hash.
4. **Dry run:** step counts match the plan, and no row changes (`ROLLED_BACK`).
5. **Approve** as approver A, then **execute LIVE** as executor B. The same person is refused (four-eyes).
6. **Verification & Audit:**
   - The outcome is **PARTIAL**: everything in the DB is verified, but the external export cannot be.
   - Coverage is shown, with per-check results.
   - Hash chain VALID, signature valid. Download the evidence and run `lae verify-evidence` offline.
7. **The research point:** pick a customer whose email was copied into `marketing_event_log` (no FK).
   - FK lineage misses it, the **residual scan finds it**, and the outcome is **FAILED**.
   - The hit appears in the Lineage Explorer as a *suggested* edge.
   - Confirm the edge, erase the next such customer: the hidden copies are now deleted.
8. **Safety:** a customer whose ticket contains another customer's message gives a *blocked* plan, and the other person's message is never deleted.

Screenshots: [docs/screenshots/](docs/screenshots/). Screenshots 01–12 were taken from the live system *before* the namesake fix and show synthetic data only. In screenshot 05 another customer's identical name made a correct erasure FAILED. It is kept as evidence of the defect we found and fixed.

## Results at a glance (measured, seed 42, 20 subjects per scale)

| | B1 manual DELETE | B2 FK-only | **LAE** |
|---|---|---|---|
| Erasure coverage (100 / 1 000 / 10 000 customers) | 0.40 / 0.46 / 0.48 | 0.32 / 0.25 / 0.24 | **0.74 / 0.75 / 0.90** |
| Shared records destroyed | 0 | 10–12 | **0** |
| Retention violations | 0 (it failed instead) | 258–331 | **0** |
| Unrelated rows damaged | 0 | 0 | **0** |
| Total latency p50 per request | 22–30 ms | 137–156 ms | 1.25–7.5 s |

![Lineage Explorer, table level](docs/screenshots/06-lineage-table.png)

After one human confirmation of a residual-scan suggestion, LAE removed 83–95 % of the hidden copies for later subjects. Details, caveats and threats to validity: [docs/benchmarking.md](docs/benchmarking.md).

## Documentation

[Architecture](docs/architecture.md) · [Database schema](docs/database_schema.md) · [Module contracts](docs/module_contracts.md) · [Methodology](docs/methodology.md) · [Algorithms](docs/algorithms.md) · [Mathematical functions](docs/mathematical_functions.md) · [API](docs/api_documentation.md) · [Testing](docs/testing.md) · [Benchmarking](docs/benchmarking.md) · [Security analysis](docs/security_analysis.md) · [Limitations](docs/limitations.md) · [Development progress](docs/development_progress.md) · [Technical walkthrough](docs/technical_walkthrough.md) · [Viva guide](docs/viva_guide.md) · [Repository audit](docs/final_repository_audit.md)

## License

No license file has been added. Until the owner adds one, all rights are reserved.
