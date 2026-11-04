# Risks and Limitations

Status: **DRAFT v0.1**. This document is an honest statement of what the prototype does **not** guarantee. It is part of the research contribution and must be kept current.

---

## 1. Critical technical risks

| # | Risk | Likelihood / Impact | Mitigation | Residual |
|---|---|---|---|---|
| R1 | **Environment:** WSL2 and Docker Desktop not installed, and installing them needs admin + a reboot. Native pgvector on Windows requires an MSVC build | High / High (blocks M1 integration tests) | User installs WSL2 + Docker Desktop (P2). Interim: portable PostgreSQL for tests. Embedding features degrade gracefully if `vector` is missing (`/health` reports it, and AT-20 is skipped with a reason) | Reproducibility across machines is weaker without containers |
| R2 | **Record-level incompleteness:** personal data copied without an FK, mapping or parseable SQL (app-level denormalization, free-text mentions, JSON blobs) is invisible to lineage | High / High | Inference suggestions. **Lineage-independent residual HMAC scan** in verification. Hits feed back as suggestions. B5 measures recall | Values transformed beyond normalization (for example, the email split into parts or a name inside prose) are not detected. LOWER_TRIM is ASCII-only, so non-ASCII case variants are missed |
| R3 | **SQLGlot coverage:** some PostgreSQL syntax and functions aren't qualified correctly, and lineage APIs change between releases | Medium / Medium | Pin the version. Classification test matrix. Failures give `OPAQUE`, which gives `BLOCKED`, never silent `KEY_PRESERVING` | Some artifacts need manual mapping |
| R4 | **Aggregates are not invertible:** you cannot "remove" a subject from `SUM` without recomputing | Certain / Medium | `REFRESH_MATVIEW` / `RECOMPUTE_TABLE`, plus `DERIVED_CONSISTENT` | Recompute cost grows with artifact size (B4) |
| R5 | **Concurrency:** rows inserted between plan and execute, or concurrent writers during execution | Medium / High | Drift re-discovery inside a SERIALIZABLE transaction, `FOR UPDATE`, lock and statement timeouts. B6 measures detection | Writers that insert the subject's data *after* commit are a new request (out of scope) |
| R6 | **Hidden cascades and triggers** in the target DB delete or modify more than planned | Medium / High | Cascades enumerated as `EXPECT_CASCADE`, rowcount assertions, `CASCADE_DESTROYS_RETAINED` conflict | **User triggers are not analyzed.** A trigger writing elsewhere is undetected (documented, flagged if `pg_trigger` is non-empty on target tables) |
| R7 | **Over-deletion:** treating dependency as ownership (deleting other customers, shared threads, products) | Medium / Critical | Downstream-only traversal. Roles `REFERENCING_OTHER_SUBJECT` / `SHARED`. Root-table protection. Human approval of hashed plan. Dry run | Policy mistakes by the operator |
| R8 | **Audit store is in the same DB:** a superuser can rewrite the entire chain consistently | Medium / High | Append-only trigger + grants. Signed evidence bundles and `CHAIN_HEAD_EXPORTED` anchors stored **outside** the DB. Offline verifier | Without an external anchor, wholesale rewrite before the first export is undetectable |
| R9 | **Audit evidence itself leaking PII** | Medium / High | HMAC-only identifiers, purge after evidence, AT-24 leakage test, no values in errors or logs | HMAC of low-entropy keys (small integer IDs) is brute-forceable **if the HMAC key leaks**. Key secrecy is essential |
| R10 | **Key management:** HMAC and signing keys live in local files or env | Medium / High | Gitignored `secrets/`. Keys never in DB or API. `signing_key.retired_at` for rotation | No HSM/KMS. Acceptable for a research prototype only |
| R11 | **Scale:** residual scan is O(all text cells). Record discovery issues one query per (edge, batch) | Medium / Medium | Batching, indexes on FK columns (the generator creates them, and discovery warns on unindexed FK columns). B1/B2 quantify | Large deployments would need sampling or indexed digests |
| R12 | **Non-determinism breaks hashes** (dict order, float formatting, timezone, platform path separators) | Medium / High | Canonical JSON rejects floats. Sorted outputs everywhere. Golden vectors. Hashing tests run in CI on Windows and Linux | |
| R13 | **Multi-agent integration drift:** parallel agents diverge from the contracts | Medium / Medium | Frozen contracts (M0), single-owner paths, change-request process, `import-linter`, Lead merges only | |

## 2. Scope limitations (by design)

1. **Physical erasure is not guaranteed.** PostgreSQL MVCC leaves dead tuples until `VACUUM`. Data persists in WAL, base backups, replicas, `pg_dump` files, OS page cache and freed disk blocks. LAE proves **logical** erasure from live relations, with optional `VACUUM`.
2. **Single PostgreSQL database.** No files, object stores, caches, search indices, message queues, analytics warehouses, or third-party processors.
3. **Embeddings:** LAE deletes embedding rows that are traceable by key. It cannot detect a subject's information *memorized inside model weights* or embedded in vectors of other documents. Exact-duplicate vector detection only.
4. **Lineage sources:** declared FKs, registered artifacts, view and matview definitions, and confirmed inferences. **Not analyzed:** application code, stored procedures, triggers, ETL jobs outside the DB, `INSERT … SELECT` history.
5. **Keys:** PK values limited to integer and text (UUID as text) in v1. Tables without a PK are handled by predicate, with weaker per-record evidence.
6. **No authentication or authorization.** `approved_by` is an unverified attestation. Single-user local prototype.
7. **Not a legal compliance tool.** Retention policies and "legal basis" are operator inputs. LAE doesn't decide what GDPR/DPDP law requires. Evidence shows *what the system did*, not that it was legally sufficient.
8. **Synchronous API.** Long operations block the request. Benchmarks use the CLI.
9. **Pseudonymization ≠ anonymization.** `PSEUDONYMIZE` replaces values with keyed HMAC-derived tokens. Quasi-identifiers in retained rows may still allow re-identification. This is reported as a `WARNING` conflict (`RETAINED_WITH_PII`), not solved.

## 3. Threats to validity (research evaluation)

- Synthetic data (the generator) may not reflect real schema irregularity. Mitigation: hidden-copy injection, a configurable depth and fan-out, and at least one hand-written "messy" schema variant.
- Ground truth is produced by the same team that wrote discovery. Mitigation: the generator is written by the DB Engineer, independently of the Discovery Engineer, from the fixture spec only.
- Laptop benchmarks (Windows host, Docker VM) have noisy timings. Mitigation: repeated runs, p50/p95, recorded environment, warm/cold separation.
