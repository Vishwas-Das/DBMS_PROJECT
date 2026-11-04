# Security Analysis

Scope: the LAE research prototype as committed (≥ 57de8e8), run locally with Docker Compose. It is **not** a production system, and nothing here is a claim of legal compliance (GDPR Art. 17, DPDP Act 2023, or any other law).

## 1. Assets and adversaries

| Asset | Why it matters |
|---|---|
| Synthetic personal data in `app` | Stands in for real personal data. Erasure must be complete *and* must not touch other people's data |
| Raw subject keys (`erasure_request.subject_key`, `plan_item.record_keys`, `record_dependency.*_key`) | Identify who asked to be erased. Kept only until evidence is issued for a VERIFIED outcome |
| HMAC master key (`LAE_HMAC_KEY`) | Protects every pseudonymous identifier in the audit trail and evidence |
| Ed25519 signing key (`secrets/signing_key.pem`) | Authenticity of evidence bundles and chain-head anchors |
| Audit chain (`lae.audit_event`) | Tamper-evident record of who did what, when |

Adversaries considered: (A1) a local user or web page that can reach the API; (A2) an operator with the API token who acts carelessly or maliciously; (A3) someone with DB access who tries to rewrite history; (A4) an auditor who receives evidence and must check it without trusting the operator's database.

## 2. Controls (what the code enforces)

| Control | Where | Evidence |
|---|---|---|
| Every SQL identifier validated against the catalog snapshot, quoted with `psycopg.sql.Identifier`. Values always bound | `lae/db/target.py`, `lae/db/_sql.py` | injection-shaped identifiers rejected (`tests/integration/db/test_target.py`) |
| Artifact SQL must be **one read-only query** (SQLGlot) and runs on the extended protocol (`prepare=True`), so multi-statement breakout is impossible | `lae/db/_sql.assert_single_select`, `PgTargetReader._exec` | `tests/unit/db/test_sql_validator.py`, `test_artifact_sql_breakout_is_rejected` |
| The app runs as a **non-superuser** `lae_app`: no UPDATE/DELETE/TRUNCATE on `audit_event`, not its owner (it cannot disable the trigger) | migration `0002_security_hardening` | `test_app_role_cannot_tamper_with_audit`. A full erasure run as `lae_app` |
| Append-only trigger (row + statement level) on `audit_event` | migration `0001_baseline` | `tests/integration/db/test_audit_sink.py`, `test_audit_events_cannot_be_modified` |
| Token on **every** route except `/api/health`. Constant-time byte compare. ≥ 24-char `SecretStr` | `lae/api/app.py`, `lae/config.py` | `test_reads_require_token_and_host_is_checked` |
| `TrustedHostMiddleware` (localhost only), explicit CORS origins, a custom header that forces a preflight | `lae/api/app.py` | same test (evil Host → 400) |
| Approval bound to the exact plan hash. Hash recomputed from stored content before approval and execution | `planner/hashing.py`, `services/pipeline.approve/execute` | `test_full_erasure_happy_path…` (wrong hash → 409) |
| **Four-eyes:** the approver may not execute (`require_four_eyes`, default on) | `services/pipeline.execute` | `test_four_eyes_and_replan_after_stale` |
| Drift check inside the SERIALIZABLE transaction before any write. `FOR UPDATE` on target rows. Rowcount assertion per step. Unplanned-cascade assertion | `executor/executor.py` | `test_drift_between_approval_and_execution_is_detected`, executor unit/property tests |
| Shared records and other subjects' rows are never deleted | `planner/planner.py` (CR erasure-1 §2) | Hypothesis property "no non-OWNED row is ever deleted". e2e unrelated-customer check |
| No raw PII in API responses, logs, errors or audit payloads | HMAC identifiers, `hide_parameters=True`, redacted error translation, validation errors that never echo input | `test_no_raw_pii_in_responses_or_logs` |
| **Per-purpose keys** (HKDF-SHA256): the audit-identity key never reaches PostgreSQL. Only the residual-probe and pseudonym subkeys do | `services/runtime.derive_subkey` | `test_hmac_subkeys_are_domain_separated` |
| Raw keys purged across **all** plan versions after a VERIFIED outcome, and at supersede time | `services/pipeline.generate_plan/issue_evidence` | `test_verified_outcome_purges_raw_subject_data`, `test_four_eyes_and_replan_after_stale` |
| Canonical subject key (read back from the DB row). Duplicate open requests refused | `services/pipeline.create_request` | `test_subject_key_is_canonical_and_duplicates_rejected` |
| Evidence verifiable **offline** (no DB, no network): Ed25519 signature, Merkle root, chain segment, column envelope | `audit/evidence.verify_evidence_offline`, `lae verify-evidence` | `tests/integration/verification/test_audit_evidence_real.py` (network blocked in-test) |
| DB published on `127.0.0.1` only | `docker-compose.yml` | config (applies on container recreate) |

## 3. Trust model: what the cryptography does and does not prove

**A valid signature proves authenticity, not erasure.** An evidence bundle that verifies offline shows:
1. the bundle was produced by the holder of the signing key whose public key you trust;
2. its content (plan hash, record-set hash, step counts, verification checks, outcome, Merkle root of per-record leaves) has not been altered since signing;
3. if a chain export is supplied, the audit chain up to the anchored event is internally consistent, and its stored columns match their hashed envelopes.

It does **not** prove:
- that every copy of the person's data is gone. It proves only that the *checks the system knew how to run* passed. Read the `verification.outcome` field: `PARTIAL` and `FAILED` bundles are just as validly signed as `VERIFIED` ones;
- anything about data outside PostgreSQL's live relations: backups, WAL, replicas, dead tuples before `VACUUM`, OS caches, log files, exports, other systems (`EXTERNAL` artifacts are only *scheduled*);
- that the operator who controls both the database and the signing key did not fabricate a consistent history from the start. Hash chains are **tamper-evident**, not tamper-proof. Detection requires an anchor kept by someone else (see §4);
- who the human was. `X-LAE-Actor` is a claim made under a single shared token. The chain proves the claim was recorded, not the identity behind it.

## 4. Residual risks (open)

| Risk | Status / mitigation |
|---|---|
| A DB superuser (`lae_owner`) can still disable the trigger and rewrite the whole chain consistently | Only detectable against an **external anchor**: `lae audit export-head` writes a signed chain head. For real assurance it must be stored *off-host* (by the auditor). Today it is written to `var/anchors` on the same machine |
| Single shared API token; actor identity self-asserted | Local-prototype auth. Per-user identities/SSO are out of scope |
| Dev-only passwords (`lae_owner_dev`, `lae_app_dev`, `lae_reader_dev`) in compose/init | Acceptable only on localhost. Override `LAE_DB_PASSWORD`. Never reuse |
| HMAC of low-entropy keys (integer customer ids) is brute-forceable **if the HMAC key leaks** | Key secrecy is essential. The audit-identity subkey never leaves the process. Probe/pseudonym subkeys are bound parameters (can appear in PostgreSQL logs if `log_statement=all`) |
| Residual scan matches whole normalised values only: free-text mentions, substrings, encodings, Unicode case variants and encrypted values are missed. With zero probes it passes vacuously | Documented (risks R2). Lineage + REDISCOVERY_EMPTY cover FK-owned free text |
| RECOMPUTE_TABLE has no rowcount assertion. The pseudonym-vs-NULL choice is not hash-bound | Detected afterwards by DERIVED_CONSISTENT / COLUMNS_ANONYMIZED. Open |
| `erasure_request.reason` and actor strings are never purged | Operators must not put personal data there. Open |
| Signing key stored unencrypted. File mode 0600 has no effect on Windows | Prototype (R10). Use an HSM/KMS in any real deployment |
| `lae erase --key 42` puts the raw key in shell history | Prefer the API/UI for real use |

## 5. Independent review

An independent, read-only review (docs/change_requests/security-review-1.md) reported 3 High, 7 Medium, 8 Low and several Info items. All High and most Medium items were fixed with regression tests on real PostgreSQL. M7 is partly fixed (four-eyes, but no per-user auth). L1, L2, L5, L6 and L8 remain open as listed above. The reviewer found these sound: identifier validation and quoting, bound values, error redaction, audit-sink locking, hash-chain encoding, Merkle domain separation (no CVE-2012-2459-style duplication), strict base64/JSON parsing, the executor's approval binding, drift check, row locking and rowcount/cascade assertions, the CORS/CSRF posture, and no committed secrets other than the documented test-vector key.

**Line coverage is not security assurance.** The tests show that the specific attacks listed above fail. They do not show that no other attack exists.
