# CR qa-devops-engineer-1: defects found by the security / e2e suite (2026-11-04)

Author: QA & DevOps Engineer. Status: **OPEN, for the Lead.** QA does not edit `backend/src/**`.
Each defect has a failing test in `backend/tests/e2e/` and a repro. Measured on HEAD 57de8e8 and
re-tested on 99082c5 against `lae_test_qa`. Both code defects reproduce 3/3 runs, so they are deterministic and not flaky.

## D1 (HIGH): the audit hash chain forks under two concurrent LIVE executions

Test: `backend/tests/e2e/test_security_db.py::test_two_threads_executing_the_same_plan_commit_exactly_once`

Repro: approve a plan, then call `pipeline.execute(rt, plan_id, LIVE, actor)` from two threads that
start together (threading.Barrier). Exactly one run COMMITS, the data is erased once and no other
customer is touched (all correct). But `POST /api/audit/verify-chain` then returns
`valid=false, first_invalid_seq=8`. Chain dump (seq, type, prev[:8], hash[:8]):

```
5 EXECUTION_STARTED   6608a993 2e243343
6 EXECUTION_STARTED   2e243343 3d48d065
7 EXECUTION_FAILED    3d48d065 abf1bf82   <- loser, separate READ COMMITTED transaction
8 EXECUTION_COMMITTED 3d48d065 7db2f6bc   <- winner, SERIALIZABLE transaction: SAME prev_hash
```

Root cause: `PgAuditSink.append` takes `pg_advisory_xact_lock` and then reads the head. Inside the
SERIALIZABLE execution transaction the snapshot was already fixed by earlier statements
(load_lineage, drift re-check, step DML). After waiting for the lock, the head SELECT still sees seq 6,
not the seq 7 that the other transaction committed in the meantime. SSI does not abort it
because the other writer is not SERIALIZABLE. The same can happen with any concurrent appender
(for example two API workers) whenever the appending transaction is REPEATABLE READ/SERIALIZABLE and
has already run a statement.

Suggested fix (needs the DB owner/Lead): (a) `UNIQUE (prev_hash)` on `lae.audit_event` (migration)
so a fork can never be committed (the loser fails closed and rolls back). (b) Also take the chain
advisory lock as the FIRST statement of any REPEATABLE READ/SERIALIZABLE transaction that will
append (service layer), so the snapshot is taken after the lock. Or append audit events of the
SERIALIZABLE transaction from a head read that is checked against `max(seq)` with
`FOR UPDATE` on a single-row chain-head table.

## D2 (MEDIUM at 57de8e8, LOW since 6cf6abb): non-integer subject keys gave HTTP 500

Update (re-tested on 99082c5): the response is now **409** `TARGET_QUERY` (a 4xx), which meets
the brief's "4xx and no damage" requirement, and the test now accepts any 4xx. Remaining (LOW):
a malformed key is invalid input, so `422 INVALID_INPUT` would be the correct status. 409 means
a state conflict. The original finding follows.

Test: `backend/tests/e2e/test_security_api.py::test_injection_shaped_subject_keys_are_rejected_without_damage`
(14 of 16 parameters fail: every non-numeric string, the empty string and a NUL byte).

Repro:
`POST /api/erasure-requests {"subject_key": ["1; DROP TABLE app.customer"]}` gives
`500 {"error":{"code":"TARGET_QUERY","message":"target query failed (SQLSTATE 22P02) on app.customer"}}`.
With a NUL byte the SQLSTATE is `null` (psycopg rejects the parameter client-side).
**No damage:** the same test checks first that all 17 app relations (row count + content hash) and
`lae.erasure_request` are unchanged, and that the hostile value is not echoed. Injection is not
possible (values are bound). The problem is only error handling: a malformed key for a `bigint`
root should be `422 INVALID_INPUT` (or `404 SUBJECT_NOT_FOUND`). It is logged at ERROR as a 500.

Suggested fix: in `create_request`, coerce/validate each key value against the catalog type of the
root key column before querying (int for integer types, reject NUL), and map SQLSTATE class 22
(data exception) from `PgTargetReader` to `ValidationError`.

## D3 (HIGH for the test suite): `.env` leaks the dev reader URL into test runtimes

Re-confirmed on 99082c5: `scripts/check.ps1` run from the repo root makes **13 of 18** tests in
`backend/tests/integration/services` fail with `404 lineage_snapshot not found` (the reader reads
the dev DB `lae`, whose snapshot ids do not exist in the test DB). The same tests pass only when
the reader URL happens to match.

`Settings(database_url=<test db>, ...)` still reads the repo `.env` when pytest runs from the repo
root, so `LAE_READER_URL=.../lae` makes the **reader engine use the dev database `lae`**:

```
owner db: lae_test_lead | reader db: lae | executor db: lae_test_lead
```

Proof (99082c5, `LAE_TEST_DATABASE_URL=.../lae_test_qa`, run from the repo root):
`pytest backend/tests/integration/services` gives **13 failed, 5 passed**. The same command with
`LAE_READER_URL` and `LAE_EXECUTOR_URL` set to the test DB in the process environment gives
**18 passed**. `scripts/check.ps1` now sets both variables per directory as a harness workaround.

Affected: `backend/tests/integration/services/test_api_e2e.py` (`env` fixture). Its GET/discovery
paths read `lae` instead of the test DB. This works only by accident, or fails with
"lineage_snapshot not found". Fix: pass `_env_file=None` and explicit `reader_url`/`executor_url`
(as `backend/tests/e2e/conftest.py::api_env` and `benchmarks/methods.py` do), or have `Settings`
ignore `.env` when `LAE_TEST_DATABASE_URL` is set.

## Notes (not defects)
- Shared records on the subject's ticket produce a non-acknowledgeable `CASCADE_DESTROYS_RETAINED` /
  `SHARED_RECORD` BLOCKING conflict, so those subjects stay BLOCKED in the benchmark (by design,
  CR erasure-1). This is reported as a measured outcome, not as a failure.
- The benchmark harness never computes probe HMACs with engine keys. Its residual scan uses its
  own random key (pgcrypto in SQL), so it stays independent of `rt.probe_keyring`.

---
## Lead decision (2026-11-05)
- **D1 (HIGH), audit chain fork under concurrency: FIXED** in two layers. (1) Migration 0003: `UNIQUE (prev_hash)` makes a fork impossible at the database level (`PgAuditSink` translates the violation into `SerializationConflict`, 503 retryable). (2) LIVE executions are serialised by a session-level advisory lock (`pipeline._live_execution_lock`) taken on a separate connection *before* the SERIALIZABLE snapshot, so the second run sees a fresh snapshot and fails cleanly. With (1) alone, *neither* run committed (safe but not live). `test_two_threads_executing_the_same_plan_commit_exactly_once` passed 3/3 after the fix (failed 3/3 before), and the full e2e suite gives 64/64.
- **D2: FIXED.** A subject key that cannot be cast to the key column type gives **422 INVALID_INPUT** (was 409 TARGET_QUERY), and the value is not echoed.
- **D3: FIXED** in the Lead's fixtures (`_env_file=None`, explicit `reader_url`). The `check.ps1` pinning stays as a belt-and-braces measure.
- mypy: clean when run as configured (`cd backend; mypy src/lae`, which uses `[tool.mypy]` in pyproject, with networkx `ignore_missing_imports`). `check.ps1` must run it from `backend/`.
- The 13 stale/mislabelled `lae.benchmark_run` rows in the dev DB were deleted by the Lead (10 at 118846f, 3 mislabelled `S2_after_feedback` at 18:36). 15 valid rows remain. The committed `benchmarks/results/summary.*` are authoritative.
- `var/` and `evidence_out/` were added to .gitignore.
- The compose `full` profile is not added: accepted (unverifiable without recreating the shared DB). CI is written, not run: accepted, and stated in the docs.
