# CR verification-crypto-engineer-1: audit primitives (M1 / VS-1)

Author: Verification & Cryptography Engineer. Status: **proposed**, needs Lead decision.
Scope: `docs/module_contracts.md` §2, §8.2; `docs/architecture.md` §12, §13; `docs/database_schema.md` §3.4.

## 1. Hash chain does not cover `entity_type`, `entity_id`, `actor`, `occurred_at` (security)
`event_hash = SHA-256(prev ‖ SHA-256(payload_canonical) ‖ seq_be64 ‖ event_type)` binds only seq,
type and payload. A DB writer could change *who* did something, *when*, or *which entity* it
concerns, and `verify_chain` would still pass.
**Proposal (no formula change, preferred):** the `AuditSink` implementation always embeds
`{"entity_type", "entity_id", "actor", "occurred_at"}` in `payload_canonical`, and
`verify_chain` (or the service) checks that the columns equal the embedded values.
**Alternative:** extend the formula with `‖ SHA-256(canonical_json({entity_type, entity_id,
actor, occurred_at}))`. That changes the golden vectors and must be decided before any event is written.

## 2. `build_evidence` input types (contract gap)
`ExecutionResult` has no `run_id` or `committed_at`, but the §13 bundle needs both. Implemented:
- `lae.audit.evidence.ExecutionEvidence(run_id: UUID, committed_at: datetime, result: ExecutionResult)`
- `lae.audit.evidence.ChainAnchor(seq: int, event_hash: str)` (with `from_event(AuditEventRecord)`)
- `build_evidence(plan, execution: ExecutionEvidence, verification: VerificationReport,
  leaves: Sequence[EvidenceLeaf], chain_anchor: ChainAnchor, signer: Signer, issued_at: datetime)`
- `snapshot_fingerprint` is taken from `plan.schema_fingerprint`.
- It refuses non-`LIVE` / non-`COMMITTED` executions, a `RUNNING` verification, naive datetimes,
  non-hex64 hashes, and any `key_hmac` that is not 64-hex (this guards against a raw key leaking into evidence).
- Leaves and checks are sorted by canonical bytes. `step_counts` = `{APPLIED, FAILED, SKIPPED}` counts.
Please record these in §8.2.

## 3. Evidence file formats (needs a decision for `/evidence/{id}/download` and the CLI)
- Export file: canonical `{"bundle": {...}, "signature": "<b64>"}`. Any other top-level key is rejected.
- Leaves file: JSON array of `EvidenceLeaf.as_canonical()` in bundle order (= `leaves_canonical`).
- Chain export: JSON array of objects. `seq, event_type, payload_canonical, prev_hash, event_hash`
  are required. `entity_type, entity_id, actor, occurred_at` are optional.
- The verifier rejects floats, NaN and **duplicate JSON keys** in every input.
The download endpoint says "bundle + leaves". If it should be one file, the Lead should define the
wrapper (for example `{"bundle", "signature", "leaves"}`), and I will make the verifier accept it.

## 4. Small signature/shape differences from §8.2
- `ChainVerification` has an extra, defaulted `reason: str | None` (machine code, no payload data).
- `verify_chain(events, expected_start_prev=GENESIS_HASH)` accepts the start value positionally
  or by keyword. That is compatible with the keyword-only form in the contract.
- `AuditEventRecord.event_type` is `str` in `types.py` but `AuditEventType` in the §2 doc. Please align them.
- Proof/verification never raise. `MerkleTree.proof` raises `ValueError` on a bad index, as the contract says.

## 5. `LOWER_TRIM` differs between Python and SQL (correctness of the residual scan)
Python `strip().lower()` removes all Unicode whitespace and uses Unicode case mapping.
Architecture §12 uses SQL `lower(trim(col))`. `trim` removes only spaces, and `lower` depends on the
DB ctype. A value with a tab, a newline or non-ASCII case would produce a probe HMAC that SQL never
matches, which is a false negative in `RESIDUAL_VALUE_SCAN`.
**Proposal:** make the normalizations identical. One option is SQL
`lower(btrim(col, E' \t\n\r\f\v'))` with a `C`/UTF-8 ctype, together with Python stripping only
those characters. I can change `normalize()` once the Lead picks the definition. `DIGITS_ONLY`
already keeps only ASCII `0-9` (= SQL `[^0-9]`).

## 6. import-linter does not enforce "audit may import only contracts, errors, cryptography"
The current contracts are `forbidden` lists. Proposal: add a `forbidden` contract for `lae.audit` →
`lae.discovery, lae.lineage, lae.planner, lae.executor, lae.verification, networkx, sqlglot, pydantic`
(or an equivalent allow-list contract).

---
## Lead decision (2026-11-04)
1. **ACCEPTED: envelope approach (no formula change, golden vectors unchanged).** `PgAuditSink` writes
   `payload_canonical = canonical_json({"envelope": {"entity_type", "entity_id", "actor", "occurred_at"}, "data": payload})`.
   The service-level chain check also asserts that the row columns equal the envelope. Owner: DB Engineer (sink), Lead (service check).
2. ACCEPTED: `ExecutionEvidence`, `ChainAnchor` and the `build_evidence` signature as implemented.
3. ACCEPTED: **separate files.** `/evidence/{id}/download` returns `{"bundle","signature"}`. `/evidence/{id}/leaves`
   returns the leaves array. CLI: `lae verify-evidence bundle.json --public-key … [--leaves …] [--chain …]`.
4. ACCEPTED. `AuditEventRecord.event_type` stays `str` in code. The doc is aligned.
5. **DECIDED: LOWER_TRIM is ASCII-only on both sides.** Strip exactly `' \t\n\r\f\v'` and lowercase only `A–Z`.
   SQL: `translate(btrim(col, E' \t\n\r\f\v'), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')`.
   This is locale-independent and byte-identical to Python. Non-ASCII case variants are a documented limitation (risk R2).
   Owner: Crypto Eng changes `normalize()` in the verification task. DB Eng uses the SQL form in `hmac_match_count`.
6. ACCEPTED: forbidden contract for `lae.audit` added to pyproject (Lead).
