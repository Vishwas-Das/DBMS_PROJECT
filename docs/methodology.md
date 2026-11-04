# Methodology: Lineage-Aware Erasure Engine (LAE)

Audience: examiners and students of a DBMS course who know SQL and Python but not this code base.
Scope: what problem the prototype addresses, how, how it is evaluated, and where the evaluation can be wrong.
Every claim about behaviour refers to code at git HEAD `118846f`, which includes the security hardening of `57de8e8` (paths are relative to the repository root).
Related: [algorithms.md](algorithms.md), [mathematical_functions.md](mathematical_functions.md), [testing.md](testing.md), [risks_and_limitations.md](risks_and_limitations.md).

> **Not a legal compliance tool, and no novelty claim.** LAE is an academic prototype. It does not decide what GDPR Art. 17 or the Indian DPDP Act 2023 require. Its evidence shows *what the system did* inside one PostgreSQL database. It does not show that this was legally sufficient. Nothing here claims patentable novelty. The contribution is an integration of known techniques, described in §4.

---

## 1. Research problem

A data subject (here: a row of `app.customer`) asks for their personal data to be erased. The obvious implementation is

```sql
DELETE FROM app.customer WHERE id = 42;
```

On the demo schema (`db/fixtures/app_schema.sql`, `db/fixtures/app_derived.sql`) this one statement is **wrong in both directions**. Some of the time it erases too little, sometimes it erases data that must be kept or that belongs to someone else, and sometimes it simply fails. The demo schema was built so that each case appears at least once.

| # | Demo object | What the naive `DELETE` does | Why it is a problem |
|---|---|---|---|
| 1 | `app.purchase_order.customer_id REFERENCES customer ON DELETE RESTRICT` | The `DELETE` **fails** with an FK violation as soon as the customer has an order | Orders are tax records under a retention policy (`ANONYMIZE`, seeded by `generator._seed_lae_config`). They must be kept, but detached from the subject and stripped of PII, not deleted |
| 2 | `app.loyalty_account` (`NO ACTION`), `app.ticket_message.author_customer_id` (`NO ACTION`) | Also make the `DELETE` fail | The engine must delete or detach these rows first, in the right order |
| 3 | `app.customer_address`, `app.document` (`ON DELETE CASCADE`) | Silently delete the rows | Acceptable for owned rows, but nothing records or checks *how many* rows went |
| 4 | `app.support_ticket.customer_id` (`ON DELETE SET NULL`) | Keeps the tickets with `customer_id = NULL`, together with every message on them | The subject's tickets and messages survive as orphans |
| 5 | `app.customer.referrer_id` (self-reference, `SET NULL`) | Nulls the reference in *other* customers' rows | Correct by luck. A traversal that followed this FK and deleted the "children" would delete other customers (customers 15 and 16 at tiny scale, 77 and 78 at scale ≥ 100) |
| 6 | Shared record: a `ticket_message` written by customer 8 on customer 7's ticket (`generator._Gen.tickets`) | If the ticket is deleted, `ticket_message.ticket_id ON DELETE CASCADE` deletes customer 8's message too | Over-deletion of another person's data |
| 7 | `app.customer_ltv` (materialized view, `GROUP BY customer_id`), `app.customer_segment` (CTAS table built from `customer_ltv`) | Rows for the subject stay until someone refreshes or recomputes them | Derived copies of personal data |
| 8 | `app.daily_revenue` (aggregate matview, no subject key) | Keeps the subject's contribution inside `SUM`/`COUNT` | No row "belongs" to the subject. The only fix is a refresh |
| 9 | `app.customer_profile_cache` (no FK, `payload jsonb` with name and e-mail) | Untouched | A cache copy that no FK points to |
| 10 | `app.document_embedding` (no FK to `document`) | Untouched | Embedding rows derived from the subject's documents |
| 11 | `app.marketing_event_log.email` (**no PK, no FK**), with exact copies of the subject's e-mail and variants such as `"  EMAIL\t"` and title-case (`generator._Gen.marketing`) | Untouched | A hidden copy. No declared relationship leads to it |
| 12 | `external.crm_export_files` (registered `EXTERNAL` artifact, outside PostgreSQL) | Untouched, and not inspectable | Must be reported as not verifiable in the database |

So erasure is a **dependency-discovery problem** (where is the data?), a **planning problem** (what action is legal and safe for each row, and in which order?), and a **verification problem** (how do we know afterwards, without trusting the same reasoning that produced the plan?).

## 2. Research questions

These are the questions the prototype is built and tested against. This document reports **no new measured results**. See §5.4 for what has been measured so far.

- **RQ1 (completeness).** How much of a subject's data, compared with an independent ground truth, does each discovery mechanism find: declared foreign keys, SQL lineage of derived artifacts, registered mappings, and inferred relationships?
- **RQ2 (safety).** Can a planner that knows record *roles* and retention *policies* avoid over-deletion: other subjects' rows, shared rows, retained rows?
- **RQ3 (verifiability).** Can a verification step that does **not** use lineage detect copies that lineage missed, and can those misses be fed back into lineage? Is the reported outcome honest (`VERIFIED` / `PARTIAL` / `FAILED`)?
- **RQ4 (evidence).** Can the result be proven to a third party, offline, with evidence that holds no personal data?
- **RQ5 (cost).** What does this cost in latency as data volume, depth and number of artifacts grow?

## 3. Approach

The approach combines several **complementary** discovery mechanisms, a policy-aware planner, a guarded executor, an **independent** verifier and signed evidence. Each mechanism covers cases the others miss.

| Stage | Mechanism | Covers demo cases | Code |
|---|---|---|---|
| Catalog | `pg_catalog` introspection, SHA-256 schema fingerprint | all (identifiers, PKs, FKs, view definitions) | `lae/discovery/catalog.py`, `lae/db/catalog_queries.py` |
| Declared lineage | FK constraints with ordered composite columns and `confdeltype` | 1–6 | `lae/discovery/foreign_keys.py` |
| SQL lineage | SQLGlot parse, qualify and column lineage, then IDENTITY/EXPRESSION/AGGREGATE classification and KEY_PRESERVING/AGGREGATE/OPAQUE traceability | 7, 8 | `lae/discovery/sql_lineage.py` |
| Registered mappings | Operator-declared key mappings for artifacts without SQL (cache, embeddings, external) | 9, 10, 12 | `ArtifactDefinition.key_mappings`, `sql_lineage._mapping_edges` |
| Inferred relationships | Name and type candidates plus sampled value containment. Always `SUGGESTED`, never traversed until a human confirms them | 11 (partly, see §6) | `lae/discovery/inference.py` |
| Record discovery | BFS over CONFIRMED, KEY_PRESERVING table edges, **downstream only**. Records come only from executed queries. Roles: `SUBJECT_ROOT`, `OWNED`, `REFERENCING_OTHER_SUBJECT`, `SHARED` | all | `lae/discovery/records.py` |
| Planning | Per-table retention policy (`DELETE` / `ANONYMIZE` / `RETAIN`) × per-record role, explicit cascades, conflicts, ordering, plan hash | 1–6, 12 | `lae/planner/` |
| Approval | A human echoes the plan hash. A different hash is rejected | – | `lae/planner/state.py: check_approval` |
| Execution | SERIALIZABLE transaction, drift re-check, row locks, rowcount assertions, cascade-count assertions, dry run | – | `lae/executor/executor.py`, `lae/services/pipeline.py: execute` |
| Verification | Per-item checks plus **lineage-independent** HMAC residual scan of every text-like column | 11 (detection) | `lae/verification/` |
| Feedback | Residual hits become `SUGGESTED` `INFERRED` edges (`evidence: residual_scan`) | 11 | `pipeline._suggest_from_residual_hits` |
| Evidence | Hash-chained audit log, Merkle tree of per-record outcomes, Ed25519-signed bundle, offline verifier, signed chain-head anchors outside the DB | – | `lae/audit/` |

### 3.1 Outcomes are reported honestly

`lae/verification/outcome.py: build_report` sets:

- `FAILED` if any check failed (including `RESIDUAL_VALUE_SCAN` and `REDISCOVERY_EMPTY`);
- `VERIFIED` only if nothing failed **and** coverage = verified items / plan items = 1;
- `PARTIAL` otherwise. For example, a `SCHEDULE_EXTERNAL` item for `external.crm_export_files` can never be verified in the database.

In the real run recorded in [api_documentation.md](api_documentation.md), subject 18 of the tiny dataset ended `PARTIAL` with 9 of 10 items verified, because of the external CRM artifact. That is the intended behaviour. It is not a failure of the engine.

## 4. Existing techniques versus this project's contribution

### 4.1 Existing techniques used (not claimed as contributions)

| Technique | Where it comes from | How LAE uses it |
|---|---|---|
| FK catalog introspection | Standard PostgreSQL system catalogs (`pg_constraint.conkey/confkey/confdeltype`) | `FOREIGN_KEYS_SQL` in `lae/db/catalog_queries.py` |
| SQL column lineage | The open-source SQLGlot library (`sqlglot.optimizer.qualify`, `sqlglot.lineage`), pinned to 30.29.0 (`tests/unit/discovery/test_sql_lineage.py: test_sqlglot_is_the_pinned_version`) | `lae/discovery/sql_lineage.py` |
| Data provenance theory (why/where provenance, provenance semirings) | Buneman, Khanna & Tan 2001. Cheney, Chiticariu & Tan 2009. Green, Karvounarakis & Tannen 2007 | Conceptual background for the three lineage levels (table, column, record) and for the IDENTITY-versus-AGGREGATE distinction. LAE does **not** implement provenance semirings or annotation propagation |
| Hash chains / linked time-stamping | Haber & Stornetta 1991 | `lae/audit/hashchain.py` |
| Merkle trees and inclusion proofs | Merkle 1987. Domain-separated leaf and node hashes as in RFC 6962 | `lae/audit/merkle.py` |
| HMAC | Bellare, Canetti & Krawczyk 1996. RFC 2104 | `lae/audit/hmac_keys.py`, pgcrypto `hmac()` in `lae/db/target.py` |
| SHA-256 | FIPS 180-4 | everywhere hashes are computed |
| Ed25519 signatures | Bernstein et al. 2012. RFC 8032 | `lae/audit/signing.py` via the `cryptography` package |
| Graph algorithms (SCC, topological sort) | Tarjan 1972. Kahn 1962. The NetworkX library | `lae/lineage/record_graph.py`, `lae/planner/ordering.py` |
| Canonical JSON for hashing | Common practice (sorted keys, no whitespace). **Not** RFC 8785 (JCS): LAE rejects floats instead of canonicalizing them | `lae/audit/canonical.py` |

### 4.2 This project's contribution: the integration

The contribution is how these pieces are combined into one erasure workflow with specific guarantees and honest limits:

1. **Record-level roles on top of table-level lineage.** Table and column edges only say where to look. Rows come from executing queries. Each row gets a role, and the root table is protected: only the subject's own root row may ever be deleted (`planner._Planner.classify`). A conservative SHARED fixpoint marks rows that also point at rows outside the subject's set (`records._Discovery.roles`).
2. **Policy-aware planning with explicit cascades.** Retention policy and role decide the action per row. Cascades are planned as `EXPECT_CASCADE` items and bound by a hash-protected `cascade_check` count. A cascade that would destroy a row the subject does not own is a BLOCKING conflict (`cascade_conflicts`). Shared rows are never destroyed, not even after acknowledgement (Lead decision in CR erasure-engine-engineer-1).
3. **Hash-bound approval.** The SHA-256 plan hash covers every item, including the human-readable reason. That reason carries the `definition_sha256` of recompute SQL and the `cascade_check` counts. Approval must echo the hash, and the executor re-hashes the stored plan (`plan_hasher=compute_plan_hash`). Since `57de8e8` a LIVE run must also come from a different `X-LAE-Actor` than the approver (four-eyes rule, `pipeline.execute`).
4. **Lineage-independent residual verification with feedback.** The residual scan looks for HMACs of the subject's identifier values in **every** text-like column of every target table and matview, without using lineage. Hits become `SUGGESTED` edges that a human can confirm, and then normal discovery deletes the copies on the next plan.
5. **Honest outcomes and PII-free evidence.** `VERIFIED` / `PARTIAL` / `FAILED` with exact coverage. Evidence (Merkle root of HMAC-only leaves, Ed25519 signature, chain anchor) can be checked offline (`lae verify-evidence`). Raw subject keys are purged only after a `VERIFIED` outcome (`pipeline.issue_evidence`).

## 5. Experimental design

### 5.1 Data: a deterministic generator with an independent oracle

`db/fixtures/generator.py` produces fully synthetic data (`@example.test` e-mails, `+1-555` phones) from one `random.Random(seed)` and a fixed base timestamp. The same seed and scale give byte-identical rows (`tests/integration/db/test_generator.py: test_in_memory_generation_is_deterministic`).

Scale presets (`PRESETS`):

| Preset | customers | orders | tickets | documents | embeddings | products | marketing events |
|---|---|---|---|---|---|---|---|
| `tiny` | 20 | 40 | 15 | 10 | 10 | 15 | 40 |
| `small` | 100 | 300 | 100 | 50 | 50 | 50 | 300 |
| `default` | 1 000 | 3 000 | 1 000 | 500 | 500 | 200 | 3 000 |
| `large` | 10 000 | 30 000 | 10 000 | 5 000 | 5 000 | 1 000 | 30 000 |

The hard cases are created on purpose for known subjects (`KnownSubjects.for_scale`): primary 7 and secondary 8 at scale < 100, primary 42 and secondary 43 otherwise. These cases are referrals, an upstream referrer, shared messages in both directions, marketing-log copies (exact, upper-case with whitespace, title-case), a cache entry, documents with embeddings, and retained orders, lines, payments and shipments.

**Ground truth** (`build_ground_truth`) is computed from the generator's own bookkeeping while it creates rows, **not** by querying FKs. That makes it an oracle that is independent of discovery. Per subject it lists `owned`, `retained`, `shared`, `referencing_other`, `derived_rows`, `aggregates` and `hidden_copies` (with EXACT and LOWER_TRIM row counts). The format is documented in `generator.FORMAT`.

### 5.2 Methods compared (baselines)

The comparison is defined by architecture.md §14 (B5) and implemented by the benchmark harness in `benchmarks/methods.py`. **Note:** at HEAD `118846f` the `benchmarks/ directory is not committed. It exists only in the working tree (untracked) while it is being written, so the details below describe the work in progress.

| Method | Description (from `benchmarks/methods.py`) |
|---|---|
| `BASELINE_1_MANUAL` | Hard-coded DELETEs of `customer_address`, `loyalty_account`, `ticket_message` (via the subject's tickets), `support_ticket`, `document` and `customer` in one transaction. No knowledge of derived artifacts, caches, embeddings, hidden copies or retention |
| `BASELINE_2_FK_ONLY` (class `FkOnlyDeletion`) | Declared-FK discovery from `lae.discovery` only, then a child-first DELETE of every discovered row. No SQL lineage, no registered mappings, no residual scan, no retention or sharing policies |
| `PROPOSED_LAE` (class `LaePipeline`) | The full `lae.services.pipeline`: snapshot → request → discovery → plan → (acknowledge) → approve → LIVE → verify → evidence |

### 5.3 Metrics

All are defined precisely in [mathematical_functions.md](mathematical_functions.md).

- **Precision, recall and F1** of the discovered record set against `owned ∪ derived_rows` of the ground truth (`lae/verification/metrics.py`, exact `Fraction`s, `None` for 0/0).
- **Residual matches** after erasure (the `RESIDUAL_VALUE_SCAN` `count`). This is a *match* count summed over normalizations, not a distinct-row count (CR verification-crypto-engineer-3 §4, accepted as a caveat).
- **Coverage** = verified items / required items, and the outcome distribution.
- **Safety violations**: other subjects', shared or retained rows that were destroyed. The e2e test checks these with direct SQL (`test_full_erasure_happy_path_checked_against_the_database`).
- **Latency** p50/p95 per phase (snapshot, discovery, plan, execute, verify, evidence). The percentile method is the one in `benchmarks/stats.py` (linear interpolation, see mathematical_functions.md §11).
- **Drift detection rate** for concurrent inserts between approval and execution (`STALE_PLAN`).

Scenarios B1–B6 are specified in architecture.md §14. Their implementation is part of the uncommitted harness. architecture.md §14 names B1 sizes of 1k/10k/100k subjects, but the largest generator preset (`large`) has 10 000 customers, so 100k would need a custom `ScaleParams`.

### 5.4 What has been measured so far (from tests, not benchmarks)

- CR lineage-discovery-engineer-2 reports precision = recall = F1 = 1 for record discovery against ground truth: all 20 tiny subjects, 20 small-scale subjects and 25 default-scale subjects (`tests/integration/discovery/test_records_ground_truth.py`). This documentation pass **collected** but did not re-run those integration tests (see testing.md).
- One real API run (tiny, seed 7, recorded in api_documentation.md) gave: subject 18 → `PARTIAL` (9/10, external artifact). Subject 9 → `FAILED`, with residual hits in `app.marketing_event_log.email` (count 6 = 3 EXACT + 3 LOWER_TRIM matches). After a residual-scan edge was confirmed, subject 6 → plan contains `DELETE_ROWS` on `marketing_event_log` by predicate, residual scan passed, outcome `PARTIAL` (13/14, external artifact).

## 6. Threats to validity

**Internal validity**
- *Same team writes generator and discovery.* The generator was written by a different role (Database Engineer) from the fixture specification. Its truth comes from bookkeeping, not FK queries. It still encodes the same *intended* semantics. One known disagreement exists: a message written by the subject on an **anonymous** ticket is `OWNED` in ground truth but `SHARED` in discovery. The generator never creates that case (CR lineage-discovery-engineer-2 §3, pinned by `test_message_on_anonymous_ticket_is_shared_documented_divergence`).
- *Ground truth and the planner agree by construction on retention.* The `retained` list in ground truth mirrors the seeded policies (`purchase_order` ANONYMIZE, `order_line`/`payment`/`shipment` RETAIN).
- *Residual counts are summed over normalizations.* A row that matches both EXACT and LOWER_TRIM counts twice, so "residual PII count" overstates rows.

**Construct validity**
- *Recall is measured against what the generator knows.* Personal data inside free text (`document.body`, `ticket_message.body`) is counted as covered when its row is deleted. The residual scan matches **whole values only**, so a name inside prose or an e-mail inside a `jsonb` document (`jsonb::text` keeps quotes) is not detected by the scan (CR verification-crypto-engineer-2 §4).
- *Inference at default θ = 0.95 does not find the marketing-log copy.* At tiny/7 containment is 17/19 (variants do not match exactly), so the ratio is 0.894 < 0.95. AT-06 as worded is therefore not met. Even a confirmed e-mail edge deletes exact copies only. Case and whitespace variants are found only by the scan (CR lineage-discovery-engineer-2 §2).
- *`LOWER_TRIM` is ASCII-only.* Non-ASCII case variants are missed (risk R2).
- *`EMBEDDING_ABSENT` is key-based only.* The vector-digest part is deferred (CR verification-crypto-engineer-2 §5).

**External validity**
- One synthetic e-commerce schema. Real schemas have more irregularity: triggers, application-maintained copies, JSON blobs. Triggers and stored procedures are not analysed (risks §2.4).
- One PostgreSQL database. Backups, WAL, replicas, files and external systems are out of scope. Erasure is **logical**, not physical (risks §2.1).

**Conclusion validity**
- Laptop timings (Windows host, Docker VM) are noisy. Benchmarks need repeated runs, p50/p95 and recorded environment. No benchmark results exist in the repository at HEAD.

## 7. References

1. Regulation (EU) 2016/679 of the European Parliament and of the Council of 27 April 2016 (General Data Protection Regulation), Article 17 "Right to erasure ('right to be forgotten')". *Official Journal of the European Union*, L 119, 4 May 2016.
2. The Digital Personal Data Protection Act, 2023 (Act No. 22 of 2023), Government of India. Cited as context only.
3. P. Buneman, S. Khanna, W.-C. Tan. "Why and Where: A Characterization of Data Provenance." In *Database Theory — ICDT 2001*, LNCS 1973, pp. 316–330. Springer, 2001.
4. J. Cheney, L. Chiticariu, W.-C. Tan. "Provenance in Databases: Why, How, and Where." *Foundations and Trends in Databases* 1(4), pp. 379–474, 2009.
5. T. J. Green, G. Karvounarakis, V. Tannen. "Provenance Semirings." In *Proc. 26th ACM SIGMOD-SIGACT-SIGART Symposium on Principles of Database Systems (PODS)*, pp. 31–40, 2007.
6. R. C. Merkle. "A Digital Signature Based on a Conventional Encryption Function." In *Advances in Cryptology — CRYPTO '87*, LNCS 293, pp. 369–378. Springer, 1988.
7. S. Haber, W. S. Stornetta. "How to Time-Stamp a Digital Document." *Journal of Cryptology* 3(2), pp. 99–111, 1991.
8. B. Laurie, A. Langley, E. Kasper. *Certificate Transparency.* RFC 6962, IETF, June 2013.
9. M. Bellare, R. Canetti, H. Krawczyk. "Keying Hash Functions for Message Authentication." In *Advances in Cryptology — CRYPTO '96*, LNCS 1109, pp. 1–15. Springer, 1996.
10. H. Krawczyk, M. Bellare, R. Canetti. *HMAC: Keyed-Hashing for Message Authentication.* RFC 2104, IETF, February 1997.
11. National Institute of Standards and Technology. *Secure Hash Standard (SHS).* FIPS PUB 180-4, August 2015.
12. D. J. Bernstein, N. Duif, T. Lange, P. Schwabe, B.-Y. Yang. "High-speed high-security signatures." *Journal of Cryptographic Engineering* 2(2), pp. 77–89, 2012.
13. S. Josefsson, I. Liusvaara. *Edwards-Curve Digital Signature Algorithm (EdDSA).* RFC 8032, IETF, January 2017.
14. R. E. Tarjan. "Depth-First Search and Linear Graph Algorithms." *SIAM Journal on Computing* 1(2), pp. 146–160, 1972.
15. A. B. Kahn. "Topological Sorting of Large Networks." *Communications of the ACM* 5(11), pp. 558–562, 1962.
16. A. A. Hagberg, D. A. Schult, P. J. Swart. "Exploring Network Structure, Dynamics, and Function using NetworkX." In *Proc. 7th Python in Science Conference (SciPy 2008)*, pp. 11–15, 2008.
17. SQLGlot, a Python SQL parser and transpiler (open-source software, github.com/tobymao/sqlglot). Version 30.29.0 is used.
