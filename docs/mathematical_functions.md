# Mathematical Functions

This document lists every mathematical or cryptographic function the prototype computes. For each one it gives the definition, purpose, formula, inputs and outputs, a small worked example, the implementing code (file:function, relative to `backend/src/lae/` unless stated otherwise), complexity, and limitations.

The worked examples marked **(computed)** were produced by running the real functions (code at HEAD `118846f`; these functions did not change in the security commit `57de8e8`) with the test key `b"0123456789abcdef0123456789abcdef"`. Digests are shortened to their first 12 to 16 hex characters for readability.

Exactness rule: nothing that is hashed or compared may contain a float. `audit/canonical.py` rejects floats, and the evaluation metrics return `fractions.Fraction`. Floats appear only at the API boundary for display: `VerificationOut.coverage` and `RelationshipEdge.confidence`.

---

## 1. Confusion counts

**Definition.** Let D be the set of records discovered by a method and T the ground-truth set from `db/fixtures/generator.py: build_ground_truth`.

$$TP = |D \cap T|,\qquad FP = |D \setminus T|,\qquad FN = |T \setminus D|$$

- **Purpose:** the basis for precision and recall (RQ1).
- **Inputs:** two sets of hashable record identifiers, e.g. `(table, pk tuple)`. **Output:** `(tp, fp, fn)`.
- **Worked example:** D = {c7, a1, a2, x9} and T = {c7, a1, a2, o5}. Then TP = 3, FP = 1 (x9), FN = 1 (o5).
- **Implementation:** `verification/metrics.py: confusion`.
- **Complexity:** O(|D| + |T|) with hash sets.
- **Limitations:** there is no true-negative count, because the universe of "all rows not belonging to the subject" is not enumerated.

## 2. Precision

$$P = \frac{TP}{TP + FP}\quad\text{(undefined, returned as None, when } TP+FP = 0\text{)}$$

- **Purpose:** the fraction of discovered rows that really are the subject's data. Low precision means over-deletion risk.
- **Inputs:** non-negative ints `tp, fp` (bools and negative values raise `ValidationError`). **Output:** `Fraction | None`.
- **Worked example:** TP = 3, FP = 1 gives P = 3/4. TP = 0, FP = 0 gives `None` (nothing was discovered).
- **Implementation:** `verification/metrics.py: precision`.
- **Complexity:** O(1).
- **Limitations:** `None` must be reported as N/A. It must not be averaged as 0 or 1.

## 3. Recall

$$R = \frac{TP}{TP + FN}\quad\text{(None when } TP+FN = 0\text{)}$$

- **Purpose:** the fraction of the subject's data that was found. Low recall means residual personal data.
- **Worked example:** TP = 3, FN = 1 gives R = 3/4.
- **Implementation:** `verification/metrics.py: recall`.
- **Limitations:** recall is measured only against what the generator records. Copies the generator does not model are not in T.

## 4. F1

$$F_1 = \frac{2PR}{P + R}\quad\text{(None if } P \text{ or } R \text{ is None, or } P + R = 0\text{)}$$

- **Purpose:** a single summary of P and R.
- **Inputs:** `Fraction` values in [0, 1] or `None`. Any other type raises `ValidationError`.
- **Worked example:** P = R = 3/4 gives F1 = (2 · 9/16)/(3/2) = 3/4. P = 0 and R = 0 gives `None`, because $2pr/(p+r)$ is 0/0. It is deliberately not 0 (CR verification-crypto-engineer-2 §2, accepted).
- **Implementation:** `verification/metrics.py: f1`.

## 5. Coverage: two deliberately different definitions

**Outcome coverage** (`contracts/types.py: VerificationReport.coverage`):

$$\mathrm{cov}_{\text{outcome}} = \begin{cases} 1 & \text{if } required = 0\\[2pt] \dfrac{verified}{required} & \text{otherwise}\end{cases}$$

**Reporting coverage** (`verification/metrics.py: coverage`):

$$\mathrm{cov}_{\text{report}}(v, r) = \begin{cases} \text{None} & r = 0\\[2pt] v / r & \text{otherwise}\end{cases}\qquad (v > r \Rightarrow \text{ValidationError})$$

- **Definitions** (`verification/outcome.py`): `required` = number of plan items (every item is required, CR lead-1 §3). `verified` = number of items whose category is not SCHEDULE, MANUAL_REVIEW or UNRESOLVED, that have ≥ 1 bound check, and whose bound checks all passed.
- **Why they differ.** The outcome coverage decides `VERIFIED`. A plan with nothing required has nothing left unverified, so it must be able to reach `VERIFIED`, hence 1. The reporting metric keeps the undefined 0/0 case visible as N/A, so that benchmark averages are not biased. Both choices were accepted by the Lead (CR verification-crypto-engineer-2 decision).
- **Worked example (real run, subject 18):** the plan had 10 items. The `SCHEDULE_EXTERNAL` item for `external.crm_export_files` is never verifiable, and the other 9 passed. So coverage = 9/10. No check failed, so the outcome is `PARTIAL`. The API shows `coverage: 0.9` as a float for display only.
- **Complexity:** O(items × checks) to compute `verified`.
- **Limitations:** coverage counts items, not rows. A 1-row item and a 1 000-row item have the same weight.

## 6. SHA-256 and canonical JSON

**Definition.** SHA-256 (FIPS 180-4) maps bytes to a 256-bit digest, shown as 64 lowercase hex characters. LAE always hashes **canonical JSON** (`audit/canonical.py: canonical_json`): UTF-8, sorted keys, separators `(",", ":")`, `ensure_ascii=False`, `allow_nan=False`. Decimal becomes a string, UUID a lowercase string, datetime an aware UTC `...Z` with microseconds, tuples lists, and enums their values. **Floats, NaN, naive datetimes and non-string keys raise `CanonicalizationError`.**

$$\mathrm{canonical\_hash}(x) = \mathrm{hex}\big(\mathrm{SHA256}(\mathrm{canonical\_json}(x))\big)$$

- **Uses:** schema fingerprint (algorithms §1), record-set hash (§5), plan hash (§8), Merkle leaves and nodes, the hash chain, `definition_sha256` in RECOMPUTE reasons, `bundle_sha256` in `EVIDENCE_ISSUED`, and signing key ids.
- **Worked example (computed):** `sha256_hex(b"abc") = ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad`, the FIPS test vector.
- **Implementation:** `audit/canonical.py: canonical_json, sha256_hex, canonical_hash`.
- **Complexity:** O(size of input).
- **Limitations:** this is not RFC 8785 (JCS). It is a project-specific canonical form, frozen by golden vectors in `backend/tests/unit/audit/vectors/`.

## 7. HMAC-SHA256 fingerprints and normalizations

**Definition** (RFC 2104):

$$\mathrm{HMAC}(K, m) = H\big((K' \oplus opad) \,\|\, H((K' \oplus ipad) \,\|\, m)\big),\qquad H = \text{SHA-256}$$

LAE computes `HMAC(K, UTF-8(normalize(v, n)))` for a value v under normalization n. For record keys it computes `HMAC(K, canonical_json(RecordKey.as_canonical()))`.

| n | normalize(v) (Python, `audit/hmac_keys.py: normalize`) | SQL twin (`db/target.py`) |
|---|---|---|
| EXACT | v (ints rendered in decimal) | `(col::text)` |
| LOWER_TRIM | strip exactly {space, TAB, LF, CR, FF, VT}, then map A–Z → a–z | `translate(btrim(col, E' \t\n\r\f\x0B'), 'A..Z', 'a..z')` |
| DIGITS_ONLY | keep ASCII 0–9 only | `regexp_replace(col, '[^0-9]', '', 'g')` |

- **Purpose:** identify subjects, records and identifier values without storing them. Used for audit payloads, evidence leaves, residual probes, API record ids and pseudonyms.
- **Key:** the master secret is `LAE_HMAC_KEY`. `config.Settings.hmac_key_bytes` uses the **UTF-8 bytes of the configured string**, which must be ≥ 32 bytes. A 64-hex string therefore gives 64 bytes; it is not hex-decoded. Since `57de8e8` the HMACs are **not** keyed with the master directly but with HKDF subkeys (§7a): the audit-identity subkey for subject, record and evidence HMACs, the residual-probe subkey for probes and the in-database scan, and the pseudonym subkey for pseudonyms.
- **Worked example (computed):** v = `"  Ivo.Juniper.9@Example.TEST\t"`. LOWER_TRIM(v) = `"ivo.juniper.9@example.test"`, and HMAC_LOWER_TRIM(v) = `095c85c4b8027bbf…`, the same digest as EXACT of the already-lowercase address. So the scan treats the padded upper-case copy as a match. DIGITS_ONLY(`"+1-555-010-9999"`) = `"15550109999"`.
- **PostgreSQL escape note:** `E'\v'` is the letter `v` in PostgreSQL, so VT must be written `\x0B` (CR verification-crypto-engineer-2 §1).
- **Implementation:** `audit/hmac_keys.py: HmacKeyring.digest, key_digest, matches` (constant-time compare). In SQL: pgcrypto `hmac(convert_to(…,'UTF8'), key, 'sha256')` in `PgTargetReader.hmac_match_count`.
- **Complexity:** O(|m|) per digest. The residual scan is O(rows × text columns × normalizations).
- **Limitations:** ASCII-only case folding. A low-entropy input (small integer key) can be brute-forced if K leaks. Whole-value equality only.

**Pseudonym** (`PgTargetWriter.update_columns`):

$$\mathrm{pseudonym}(row) = \texttt{"anon\_"} \,\|\, \mathrm{hex}\big(\mathrm{HMAC}(K, pk_1\!:\!pk_2\!:\!\dots)\big)[0{:}16]$$

Here K is the pseudonym subkey (§7a). It is deterministic per row and not reversible without K. Verification accepts a value as anonymized iff it is NULL or fully matches `anon_[0-9a-f]{16}` (`verification/checks.py: is_anonymized`).

## 7a. HKDF-SHA256 subkey derivation

**Definition** (HKDF, RFC 5869, with SHA-256; `services/runtime.py: derive_subkey`):

$$PRK = \mathrm{HMAC}(salt,\ master),\qquad K_{purpose} = T(1) = \mathrm{HMAC}\big(PRK,\ info \,\|\, \texttt{0x01}\big)\ \ (\text{32 bytes, so one block})$$

with $salt$ = `b"lae-hkdf-salt-v1"` and $info$ = the ASCII purpose string: `lae/audit-identity/v1`, `lae/residual-probe/v1` or `lae/pseudonym/v1`.

- **Purpose:** domain separation. A digest made under one purpose cannot be matched or reproduced with another subkey. The audit-identity subkey is used only in Python and is never sent to PostgreSQL. The probe and pseudonym subkeys must be sent, as bound parameters, because the database computes those HMACs.
- **Inputs:** master bytes (≥ 32) and a purpose. **Output:** 32 bytes.
- **Worked example:** with the same master, `derive_subkey(m, "lae/residual-probe/v1")` ≠ `derive_subkey(m, "lae/pseudonym/v1")`, so a probe digest of an e-mail differs from any value derivable with the pseudonym key. Asserted by `tests/integration/services/test_security_hardening.py: test_hmac_subkeys_are_domain_separated`.
- **Implementation:** `cryptography.hazmat.primitives.kdf.hkdf.HKDF`, called once per purpose in `Runtime.from_settings`.
- **Complexity:** O(1).
- **Limitations:** compromise of the master compromises all subkeys. Rotating the master changes every HMAC, so old requests, probes and leaves can no longer be matched.

## 8. Hash-chain recurrence

**Definition** (`audit/hashchain.py: next_hash`):

$$h_0 = 0^{64}\ (\text{64 ASCII zeros}),\qquad h_i = \mathrm{SHA256}\big(\mathrm{hexdecode}(h_{i-1}) \,\|\, \mathrm{SHA256}(\mathrm{UTF8}(p_i)) \,\|\, \mathrm{be64}(s_i) \,\|\, \mathrm{UTF8}(t_i)\big)$$

Here $p_i$ = `payload_canonical` (envelope + data), $s_i$ = `seq` as an unsigned 64-bit big-endian integer, and $t_i$ = `event_type`.

- **Purpose:** tamper evidence for the audit log. Changing, removing, inserting or reordering an event breaks the chain at that event.
- **Inputs:** previous hash (64 hex), seq ∈ [0, 2^64), non-empty event type, payload string. **Output:** 64 hex.
- **Worked example (computed):** with $p$ = `{"data":{},"envelope":{}}`: $h_1$ = next_hash($0^{64}$, 1, `SNAPSHOT_CREATED`, p) = `4736a69a44d6…`, and $h_2$ = next_hash($h_1$, 2, `REQUEST_RECEIVED`, p) = `f86f35819228…`. Changing one byte of $p$ at seq 1 changes $h_1$, so seq 2's stored `prev_hash` no longer matches. `verify_chain` returns `first_invalid_seq = 1`.
- **Verification rule** (`verify_chain`): for each event in order, require seq strictly increasing (gaps allowed), `prev_hash` = previous `event_hash`, and recomputed hash = stored hash.
- **Complexity:** O(N) for N events.
- **Limitations:** a party that can rewrite *all* rows can recompute a consistent chain. External signed anchors are the countermeasure (algorithms §17).

## 9. Merkle tree with domain separation and inclusion proofs

**Definition** (`audit/merkle.py`), in the style of RFC 6962:

$$\mathrm{leaf}(x) = \mathrm{SHA256}(\texttt{0x00} \,\|\, \mathrm{canonical\_json}(x)),\qquad \mathrm{node}(L, R) = \mathrm{SHA256}(\texttt{0x01} \,\|\, L \,\|\, R)$$

Levels are built by pairing left to right, and an odd last node is promoted unchanged. The empty root is $\mathrm{SHA256}(\varepsilon)$.

The 0x00 and 0x01 prefixes provide **domain separation**: a leaf hash can never be confused with an internal node, which prevents second-preimage tricks where an internal node is presented as a leaf.

**Inclusion proof** for leaf i: the list of (sibling hash, side). Verification folds the list:

$$c_0 = \mathrm{leaf}(x),\qquad c_{j+1} = \begin{cases}\mathrm{node}(s_j, c_j) & \text{side}=L\\ \mathrm{node}(c_j, s_j) & \text{side}=R\end{cases},\qquad \text{accept iff } c_k = \text{root} = \text{proof.root}$$

- **Purpose:** an auditor can confirm that "record X (by HMAC) was erased and verified" from the root plus about log₂ n hashes, without seeing the other records.
- **Worked example (computed):** three leaves with `key_hmac` = `a…a`, `b…b`, `c…c`. Leaf hashes: `1af8c33c352a…`, `5fa4c247a731…`, `f80a4725cd2e…`. Level 1: `node(l0,l1) = 86a65ed888d6…`, and `l2` is promoted unchanged (`f80a4725cd2e…`). Root = `node(86a6…, f80a…) = 261d3755dc19…`. The proof for leaf 2 has a single step `(86a65ed888d6…, L)` and verifies (`True`).
- **Implementation:** `audit/merkle.py: leaf_hash, node_hash, MerkleTree.build/proof, verify_proof`. Leaves come from `audit/evidence.py: leaves_from_verification` and are sorted by canonical bytes.
- **Complexity:** build O(n). Proof size and verification O(log n).
- **Limitations:** a proof shows a leaf is *in* the signed set. It does not prove that the set is complete. Completeness rests on the plan hash and the verification checks.

## 10. Ed25519 signatures

**Definition** (Bernstein et al. 2012, RFC 8032). Ed25519 is a Schnorr-type signature over the twisted Edwards curve equivalent to Curve25519, using SHA-512. As used here:

$$\sigma = \mathrm{Sign}_{sk}\big(\mathrm{canonical\_json}(\text{bundle})\big),\qquad \mathrm{Verify}_{pk}(\text{msg}, \sigma) \in \{\text{true}, \text{false}\}$$

$\sigma$ is 64 bytes, transported as standard base64 with padding. `key_id` = first 16 hex characters of SHA-256(raw 32-byte public key).

- **Purpose:** authenticity and integrity of evidence bundles and chain-head anchors, checkable offline.
- **Implementation:** `audit/signing.py: Signer.sign`, `verify_signature` (rejects wrong length and non-canonical base64, and never raises for a bad signature), `key_id_for`. The private key is unencrypted PKCS#8 PEM (`lae keys generate`, refuses to overwrite).
- **Worked example (real run):** the downloaded bundle verified with `lae verify-evidence` (`signature_ok: true`). Changing the anchor seq from 73 to 72 made `signature_ok: false`, because the canonical bytes changed.
- **Complexity:** constant time per signature or verification (independent of message length apart from hashing).
- **Limitations:** keys live in files (no HSM or KMS, risk R10). Ed25519 signing is deterministic, so the same bundle bytes always give the same signature.

## 11. Inferred-relationship confidence

**Definition** (`discovery/inference.py: infer_relationships`). For a candidate child column c and parent column p with name weight w:

$$S = \text{sample\_values}(c, 1000)\ (\text{distinct, non-NULL}),\qquad \rho = \frac{|\{v \in S : v \in p\}|}{|S|}\quad(\text{exact text comparison})$$

$$\text{suggest iff } \rho \ge \theta\ (\theta = 0.95),\qquad \mathrm{conf} = \big\lfloor \min(\rho \cdot w,\ 0.999) \big\rfloor_{0.001}$$

Here $\lfloor\cdot\rfloor_{0.001}$ means rounding down to 3 decimals (`Decimal.quantize(..., ROUND_DOWN)`). $\rho$ and $w$ are computed exactly as `Decimal`, and `conf` is converted to `float` at the end.

- **Weights $w$:** `_id` patterns use 1 (exact name), 0.98 (plural) and 0.95 (suffix). `email` patterns use 0.95 (stem names the parent) and 0.9 (otherwise).
- **Cap and floor:** the 0.999 cap keeps INFERRED edges strictly below 1. That satisfies the DB check `inferred_confidence` (`origin <> 'INFERRED' OR confidence < 1 OR status <> 'SUGGESTED'`, migration `0001_baseline`) and fits `numeric(4,3)`. Rounding down never overstates confidence.
- **Worked example (from CR lineage-discovery-engineer-2 §2, tiny/7):** for `marketing_event_log.email → customer.email`, 17 of 19 distinct sampled values are contained. $\rho = 17/19 = 0.8947…$. At θ = 0.95 it is **not suggested**. At θ = 0.8 it is suggested with conf = ⌊0.8947 × 0.9⌋ = ⌊0.8052…⌋ = **0.805**. A perfect match ($\rho = 1$, w = 1) is capped to 0.999.
- **Residual-scan suggestions** are created with a fixed `confidence = 0.5` (`services/pipeline.py: _suggest_from_residual_hits`). They are not computed by this formula.
- **Complexity:** 2 queries per candidate.
- **Limitations:** the threshold is applied to $\rho$, not to conf. Exact-text containment misses normalized variants.

## 12. Latency percentiles (p50, p95)

**Definition.** The benchmark harness file `benchmarks/stats.py: percentile` uses linear interpolation between closest ranks (numpy's default `'linear'` method). For sorted values $x_0 \le \dots \le x_{n-1}$ and quantile q:

$$pos = (n-1)\,q,\quad lo = \lfloor pos \rfloor,\quad hi = \min(lo+1, n-1),\quad Q(q) = x_{lo} + (x_{hi}-x_{lo})(pos - lo)$$

It returns `None` for n = 0 and $x_0$ for n = 1. `describe` reports `p50 = Q(0.50)` and `p95 = Q(0.95)` rounded to 3 decimals, plus n, mean, min and max.

- **Worked example:** timings [10, 20, 30, 40, 50] ms. p50: pos = 2, giving 30. p95: pos = 3.8, giving 40 + 0.8 × 10 = 48 ms.
- **Use:** the committed harness (`benchmarks/stats.py`) applies it to 20 subjects per set.
- **Complexity:** O(n log n) for the sort.
- **Limitations:** with 20 subjects per set (fewer when plans are blocked), p95 interpolates between the largest samples and is statistically weak.

## 13. Plan hash, record-set hash and fingerprint as functions

These are covered in algorithms.md §1, §5 and §8. All three are `canonical_hash` of a sorted structure, so they are independent of input order. This is tested in `tests/unit/planner/test_hashing_state.py: test_hash_is_stable_across_processes`, `tests/unit/lineage/test_record_graph.py: test_record_set_hash_stable_across_insertion_order` and `tests/unit/discovery/test_catalog.py: test_fingerprint_is_order_independent`.
