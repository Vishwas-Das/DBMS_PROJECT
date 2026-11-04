# Security review 1: independent read-only review (2026-11-04) and resolutions

Reviewer: an independent general-purpose agent, given the code but not the Lead's conclusions. Read-only. Its findings are summarised here with their resolution. "Test" names the regression test that proves the fix on real PostgreSQL.

| ID | Sev | Finding | Resolution | Test |
|---|---|---|---|---|
| H1 | High | DB port published on all interfaces; superuser with default password | **Fixed:** compose binds `127.0.0.1:5433`. *Takes effect on container recreate* (`docker compose up -d db`). Dev passwords remain (documented, local only) | config |
| H2 | High | Unauthenticated GETs expose subject ids/names, request linkage, audit payloads. LIKE wildcards enumerate. DNS rebinding | **Fixed:** token required on every route except `/api/health`. `TrustedHostMiddleware`. LIKE `%`/`_`/`\` escaped | `test_reads_require_token_and_host_is_checked`, `test_like_wildcards_do_not_enumerate_customers` |
| H3 | High | Raw keys survive in SUPERSEDED plan versions | **Fixed:** raw keys purged when a plan is superseded, and across all versions at evidence time | `test_four_eyes_and_replan_after_stale` (asserts 0 raw keys in the superseded plan) |
| M1 | Medium | Artifact SQL spliced as text; simple-query protocol allows multi-statement breakout | **Fixed:** `assert_single_select` (SQLGlot: exactly one query, no DML/DDL/transaction/DML-CTE/INTO), plus `prepare=True` (extended protocol rejects multiple statements) | `tests/unit/db/test_sql_validator.py` (14), `test_artifact_sql_breakout_is_rejected` |
| M2 | Medium | App runs as cluster superuser, so it could disable the audit trigger | **Fixed:** non-superuser `lae_app` role (migration 0002): owns `app`, has DML on `lae`, but **no UPDATE/DELETE/TRUNCATE on audit_event, and is not its owner**. The full pipeline was verified running as `lae_app` | `test_app_role_cannot_tamper_with_audit`. A full erasure run as `lae_app` (PARTIAL 9/10, offline evidence valid) |
| M3 | Medium | One HMAC key for all purposes, sent to PostgreSQL | **Fixed:** HKDF-SHA256 subkeys: `audit-identity` (never sent to the DB), `residual-probe`, `pseudonym`. Golden vectors unaffected | `test_hmac_subkeys_are_domain_separated` |
| M4 | Medium | `"42"`/`"042"`/`42` give different subject HMACs; duplicate open requests | **Fixed:** the key is canonicalised from the DB row. A second open request for the same subject gives 409 | `test_subject_key_is_canonical_and_duplicates_rejected` |
| M5 | Medium | **Functional bug:** superseding an APPROVED plan violated `ck_erasure_plan_approval_consistent`, so after STALE_PLAN the request was stuck | **Fixed:** migration 0002 lets SUPERSEDED/FAILED keep their approval history. Re-plan works | `test_four_eyes_and_replan_after_stale` |
| M6 | Medium | Unauthenticated GETs trigger expensive work and create snapshots | **Fixed:** GETs never create snapshots (`latest_snapshot`, 404 before the first discovery). All GETs need the token | covered by H2 tests |
| M7 | Medium | Actor self-asserted; approver can execute | **Partly fixed:** four-eyes rule (`require_four_eyes`, default on): approver ≠ executor. **Remaining:** identity is still a header claim under one shared token (no per-user auth), documented | `test_four_eyes_and_replan_after_stale` |
| L1 | Low | RECOMPUTE_TABLE has no rowcount check / target guard | **Open (documented):** DERIVED_CONSISTENT verification detects a wrong recompute afterwards | — |
| L2 | Low | Pseudonym-vs-NULL choice not bound by the plan hash | **Open (documented)** | — |
| L3 | Low | Non-domain exceptions leave a run RUNNING. `assert` stripped under -O | **Fixed:** generic exceptions become `EXECUTION_FAILED`, recorded with an audit event. The assert is replaced by an explicit check | full regression |
| L4 | Low | `compare_digest(str)` 500s on non-ASCII. No token length minimum. Token in `repr` | **Fixed:** bytes compare. `api_token` is a `SecretStr` with ≥ 24 chars | `test_reads_require_token_and_host_is_checked` |
| L5 | Low | Residual scan passes vacuously with zero probes. Per-table legal-hold masking | **Open (documented)** | — |
| L6 | Low | `reason` free text and actor strings never purged | **Open (documented)** | — |
| L7 | Low | Unbounded key strings/ints/subject_root, `/benchmarks` | **Fixed:** key items bounded (int64 range, str ≤ 128), `subject_root` pattern + length, scenario length | API validation |
| L8 | Low | `lae_reader` password hard-coded in init | **Open:** dev-only and documented |
| Info | — | The offline verifier's `valid` means *authentic*, not *erasure succeeded*: callers must read the outcome. Anchors are written to the same host. Raw key on the CLI command line. Unencrypted PEM on Windows | Documented in docs/security_analysis.md | — |

The reviewer's "checked and found sound" list (identifier validation/quoting, bound values, error redaction, audit-sink locking, hash-chain encoding, Merkle domain separation, strict signature/JSON parsing, executor approval binding, drift check, FOR UPDATE locking, rowcount + cascade assertions, CORS/CSRF posture, no committed secrets except the documented test key) is reproduced in docs/security_analysis.md.
