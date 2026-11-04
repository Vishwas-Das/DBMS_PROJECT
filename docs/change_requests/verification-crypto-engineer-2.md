# CR verification-crypto-engineer-2: verification engine (stage 9) and follow-ups

Author: Verification & Cryptography Engineer. Status: **proposed**, needs Lead decision.
Scope: `docs/module_contracts.md` §3, §8.1, §8.2; `docs/architecture.md` §12, §13; CR verification-crypto-engineer-1 §5 decision.

## 1. The decided SQL for LOWER_TRIM is wrong: `E'\v'` is not a vertical tab (correctness, urgent for the DB Engineer)
PostgreSQL escape strings support only `\b \f \n \r \t`, octal, `\x`, `\u` and `\U`. "Any other character following a backslash is taken literally", so `E' \t\n\r\f\v'` contains the **letter `v`**, not U+000B.
`btrim(col, E' \t\n\r\f\v')` would strip leading and trailing `v` characters (`'vivian'` → `'ivian'`) and keep VT. That gives false negatives in `RESIDUAL_VALUE_SCAN`.
**Fix:** `translate(btrim(col, E' \t\n\r\f\x0B'), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')` (or `\013`).
Python `normalize()` now strips exactly `LOWER_TRIM_STRIP_CHARS` (space, TAB, LF, CR, FF, VT = U+000B) and lowercases only A-Z. The integration test for `hmac_match_count` should include `'vivian'` and a VT-wrapped value.

## 2. `metrics.coverage(0, 0)` is `None` but `VerificationReport.coverage` is `1`
The task asked for 0/0 → None in all of `lae.verification.metrics`. CR lead-1 §3 defines coverage = 1 when required = 0, and `VerificationReport.coverage` implements that. I kept both: the outcome (`VERIFIED`) uses the contract property, and `metrics.coverage` is the reporting metric. Please confirm, or tell me to make `metrics.coverage(0, 0)` return `Fraction(1)`.
`f1(p, r)` with `p + r == 0` is also `None` (0/0), not 0.

## 3. Assumptions `verify_execution` makes about its inputs (please record in §8.1)
- **Preconditions:** `execution` must be `LIVE` + `COMMITTED`, and `hmac_key` ≥ 32 bytes. Otherwise it raises `VerificationError`.
- **Key-based actions must be RECORD-level:** DELETE_ROWS, EXPECT_CASCADE, DELETE_DERIVED_BY_KEY, INVALIDATE_CACHE, ANONYMIZE_ROWS, NULLIFY_REFERENCE and RETAIN_ROWS at TABLE level raise `VerificationError(code="UNCHECKABLE_ITEM")`. (Planner engineer: please confirm.)
- **REFRESH_MATVIEW / RECOMPUTE_TABLE** need an artifact in `artifacts` whose `target` is the item table and that has a `definition_sql`. Two artifacts with the same target → `AMBIGUOUS_ARTIFACT`.
- **Key-preserving artifacts** are artifacts with `key_mappings`. The subject's derived keys are taken from the DELETE-category items on `mapping.source`, projected onto `mapping.source_columns` (which must be a subset of the item's `key_columns`, otherwise that mapping is skipped). A RECORD-level refresh item with keys uses its own keys instead.
- **EMBEDDING_ABSENT** runs for every `EMBEDDING_TABLE` artifact with derivable source keys, **even if the plan has no item for it**. It is bound to the first item on that table if one exists, otherwise `step_order=None`.
- **REDISCOVERY_EMPTY:** `rediscover_records` raising `SubjectNotFoundError` counts as "nothing rediscovered" (the root row is gone). Discovery must raise exactly that error when the root is missing. Any other exception → `VerificationError`.
- **REFERENCE_NULLIFIED:** a referencing row that no longer exists passes only if a DELETE-category item of the same plan removes it (a cycle break). Otherwise it is `rows_missing` and the check fails.
- **COLUMNS_ANONYMIZED / RETENTION_HONORED** also require `rows_present == expected_row_count`.
- `VerificationError.code` values: `NOT_VERIFIABLE`, `INVALID_HMAC_KEY`, `UNCHECKABLE_ITEM`, `AMBIGUOUS_ARTIFACT`, `INVALID_PROBE`, `CHECK_NOT_RUNNABLE`, `INVALID_READER_RESULT`. Reader exceptions are **not chained** (`from None`), because PostgreSQL messages can quote row values (`DETAIL: Key (email)=(…)`). `details.cause` holds only the exception class name.

## 4. Residual scan limits (please add to risks_and_limitations.md)
- **Whole-value matching only.** HMAC matching finds a column value that, after normalization, *equals* a probe value. A copy embedded in free text (for example `app.document.body` containing the customer's name and email, CR lead-1 §5) is **not** found. Also, `jsonb::text` keeps quotes and keys, so an email inside JSON does not match. A substring scan would need raw probe values as bound parameters inside the reader transaction, before the purge. That is a design decision for the Lead.
- **Legal hold exemption is per table.** Hits in a table whose policy is `RETAIN` *and* that has a `RETAIN_ROWS` item go to `retained_hits` and do not fail. A count cannot be attributed to a row, so an extra hidden copy in that same table would be masked. A precise option: add a port method `hmac_match_count(..., exclude_keys=(key_columns, keys))`.
- Non-ASCII case variants are not matched (R2, as decided).

## 5. EMBEDDING_ABSENT vector-digest part is not implemented (contract gap)
Architecture §12 also requires "no stored vector with the same `sha256(vector::text)` as the pre-erasure vectors (digests captured at planning time)". Neither the inputs of `verify_execution` nor `TargetReader` provide those digests. Proposal: add `vector_digests: Mapping[TableRef, Sequence[str]]` to `verify_execution`, plus a port method `vector_digest_match_count(table, column, digests) -> int`. Until then only the key-based part runs.

## 6. Evidence leaves and `is_predicate` (contract gap)
`PlanItem` has no `is_predicate`, so `leaves_from_verification` digests keys as `RecordKey(table, key_columns, values, is_predicate=False)`. For tables without a PK that differs from the `RecordGraph` key digest. Proposal: add `PlanItem.is_predicate: bool = False`, or accept this rule. TABLE-level items get one leaf with `key_hmac = HMAC(table qualified name, EXACT)`. RECORD-level items with no keys produce no leaf.

## 7. New: signed external chain-head anchors (`lae.audit.anchor`, record in §8.2)
- `export_chain_head(head: ChainAnchor | AuditEventRecord, signer, exported_at) -> SignedChainHead`. The document is canonical `{"anchor_version":"1","seq","event_hash","exported_at","key_id"}`. The file is canonical `{"anchor": {...}, "signature": b64}`.
- `verify_anchor(anchor_json, public_key_pem, chain: str | Sequence[AuditEventRecord] | None = None) -> AnchorVerification(valid, signature_ok, chain_ok, seq, event_hash, errors)`. It never raises and uses no DB. With `chain`, the segment must be valid and contain the anchored `(seq, event_hash)`. A consistent rewrite of the chain is detected.
- Suggested service use (Lead): append `CHAIN_HEAD_EXPORTED` with payload `{seq, event_hash, key_id, anchor_sha256}`. Optional CLI `lae audit export-head --out head.json` / `lae audit verify-head head.json --public-key … [--chain …]`.

## 8. import-linter
`lae.verification` imports `lae.contracts`, `lae.errors`, `lae.lineage.results`, `lae.lineage.record_graph` (transitively) and `lae.audit.hmac_keys` only. I suggest adding an explicit forbidden contract: `lae.verification` → `lae.discovery, lae.planner, lae.executor`.

---
## Lead decision (2026-11-04)
- §1 `\v` bug: **ACCEPTED, fixed.** CR-1 §5 corrected. The DB Engineer was notified while implementing `hmac_match_count`.
- `metrics.coverage(0,0) = None` (benchmarks report N/A) vs `VerificationReport.coverage = 1` (nothing required means nothing left unverified): both ACCEPTED, with distinct purposes, documented in docs/mathematical_functions.md. `f1 = None` when p + r = 0: ACCEPTED.
- §3 UNCHECKABLE_ITEM: ACCEPTED. The planner emits RECORD-level items for key-based actions. Refresh/recompute artifacts always carry definition_sql (auto-registered matviews take it from pg_get_viewdef).
- §4 whole-value scan + per-table legal-hold masking: ACCEPTED as **documented limitations** (risks R2). Free-text copies inside an FK-owned row (e.g. `document.body`) are covered by lineage (the row is deleted) and by REDISCOVERY_EMPTY, not by the scan.
- §5 vector-digest check: deferred. EMBEDDING_ABSENT is key-based only. Documented.
- §6 predicate keys in leaves: ACCEPTED as is.
- §7 anchors in `lae.audit.anchor`: ACCEPTED. The CLI `lae audit export-head` and the `CHAIN_HEAD_EXPORTED` event are wired by the Lead.
