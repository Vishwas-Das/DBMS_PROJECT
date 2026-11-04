# CR verification-crypto-engineer-3: stage 9/10 integration on real PostgreSQL

Author: Verification & Cryptography Engineer. Status: **proposed**, needs Lead decision.
Scope: `docs/module_contracts.md` §3 (TargetReader), §8.2 (audit); `lae.db.repositories.audit`
(Database Engineer); `backend/tests/integration/conftest.py` (Database Engineer).

Context: `backend/tests/integration/verification/**` now runs `verify_execution` with the real
`PgTargetReader` / `PgTargetWriter` on the seeded tiny/7 dataset (database `lae_test_verif`), and
runs the DB-backed audit chain through `PgAuditSink` / `AuditRepo` into `build_evidence` and the
offline verifiers. No functional bug was found in `PgTargetReader` / `PgTargetWriter` /
`PgAuditSink`. The items below are one adapter defect (low severity), one test-fixture gap, and
two contract additions.

## 1. `AuditRepo.export_chain` writes non-canonical `occurred_at` text (DB Engineer, low)

`backend/src/lae/db/repositories/audit.py`, `export_chain`:

```python
"occurred_at": e.occurred_at.isoformat().replace("+00:00", "Z"),
```

- `isoformat()` without `timespec` drops the fraction when `microsecond == 0`
  (`2026-11-04T10:00:00Z`), while the hashed envelope written by `PgAuditSink` uses
  `canonical_json` (`...T10:00:00.000000Z`).
- If the DB session `TimeZone` is not UTC, psycopg returns the session offset
  (`...+05:30`), so the `replace` does not apply and the export is not in UTC.

Both still denote the same instant, so the offline verifier (which compares instants, see §3)
accepts them. But the export is meant to be a stable, diffable artifact. **Fix:**
`e.occurred_at.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")`,
which is exactly the `lae.audit.canonical` datetime form.

## 2. Integration DB guard accepts only `lae_test` (DB Engineer, test fixtures)

`backend/tests/integration/conftest.py` fails any database other than `lae_test`, so specialists
that are given their own database (`lae_test_verif`, `lae_test_disc`, ...) cannot use the shared
fixtures. I override `migrated_url` in `backend/tests/integration/verification/conftest.py`
(allows exactly `lae_test` and `lae_test_verif`). Proposal: accept `lae_test` and `lae_test_*`
in the shared guard and drop my override.

Caveat for full-suite runs: `migrated_db` and `_dataset_state` are session-scoped, so all
integration tests of one pytest session must use the same database URL.

## 3. Offline verifiers now cross-check the exported envelope columns (record in §8.2)

`entity_type`, `entity_id`, `actor` and `occurred_at` are columns that the event hash does not
cover. `PgAuditSink` also stores them in the hashed `payload_canonical` envelope
(CR verification-crypto-engineer-1 §1). The integration test shows that a superuser rewrite of
`actor` or `occurred_at` keeps `verify_chain` **valid**. So the hash chain alone cannot detect it.

New in `lae.audit.evidence` (pure, no DB):

- `envelope_mismatch(columns: Mapping[str, Any], payload_canonical: str) -> bool`: compares each
  column that is present in both the export and the envelope. Timestamps are compared as instants
  and UUIDs case-insensitively. Payloads without an envelope are not compared.
- `first_envelope_mismatch(events: Sequence[AuditEventRecord]) -> int | None`
- `chain_segment_error(events, envelope_bad_seq, anchor_seq, anchor_hash, *, not_found=...) -> str | None`

`verify_evidence_offline(chain_json=...)` and `verify_anchor(chain=...)` report
`"audit chain invalid at seq N (envelope_mismatch)"` for the first bad `seq` (hash-chain failure
or envelope mismatch, whichever comes first). No golden vector changed. The vector chain events
have no envelope, so they are not compared.

**Request (Lead / services):** `lae audit verify` and `GET /audit/verify` should also call
`first_envelope_mismatch(AuditRepo.chain())`. Without that, the DB-side verification misses
column rewrites that the offline verifier catches.

## 4. Residual-scan counts are match counts, not row counts (TargetReader §3, Lead)

`RESIDUAL_VALUE_SCAN` calls `hmac_match_count` once per normalization and sums the results. A row
that matches under several normalizations is counted more than once. On real data (subject 7),
`app.marketing_event_log.email` has 5 copy rows: 3 exact and 2 case/whitespace variants.
The scan reports `count = 8` (EXACT 3 + LOWER_TRIM 5, because LOWER_TRIM includes the exact
copies). Pass/fail is correct. However, the benchmark metric "residual PII count"
(architecture §14 B5) would overstate rows.

Proposal: add a port method
`hmac_match_rows(col, digests_by_normalization: Mapping[Normalization, Sequence[str]], hmac_key) -> int`
that counts rows matching **any** normalization in a single `WHERE ... OR ...` query. The
`RESIDUAL_VALUE_SCAN` `count` would then be distinct rows. Until that is decided, the
`[{table, column, count}]` shape stays as it is, and `count` is documented as "matches summed over
normalizations".

## 5. Observations (no action needed)

- `PgTargetReader._read_only` (savepoint + `SET TRANSACTION READ ONLY`, rolled back) does not
  leave the caller's transaction read-only: DML on the same connection after
  `symmetric_difference_count` succeeds.
- `symmetric_difference_count` works with the registered `pg_get_viewdef` text of
  `app.customer_ltv` / `app.daily_revenue` and with the CTAS definition of
  `app.customer_segment`. All three are stale after deleting payments (difference 1 / ≥ 2 / 1) and
  consistent after REFRESH / recompute.
- `PgTargetWriter.update_columns` PSEUDONYM values equal
  `'anon_' + hex(HMAC-SHA256(key, pk::text))[:16]` recomputed in Python. They match
  `^anon_[0-9a-f]{16}$` both in SQL and in `lae.verification.PSEUDONYM_PATTERN`.

---
## Lead decision (2026-11-04)
- §1 `export_chain` timestamp: FIXED by the Lead (`astimezone(UTC).isoformat(timespec="microseconds")`).
- §2 guard widened to `lae_test_<suffix>` (Lead). The local override may be removed later.
- §3 ACCEPTED, done: `LaeServices._chain_out` (API `/api/audit/verify-chain`, `/api/audit/{id}`, CLI `lae audit verify`) now also applies `first_envelope_mismatch`.
- §4 `hmac_match_rows` (distinct-row counts): ACCEPTED as a known metric caveat. Benchmarks report residual *matches*, labelled as such. The port method is deferred.
