# Limitations

This page lists what LAE does **not** do, could not verify, or does only partly. Each item states where it comes from (design scope, measured result, or open review finding). Design-time risk analysis is in [risks_and_limitations.md](risks_and_limitations.md). Security-specific residual risks are in [security_analysis.md §4](security_analysis.md).

## 1. What "erased" means here
1. **Logical, not physical, erasure.** Rows are removed from live relations. Copies may persist in PostgreSQL dead tuples until `VACUUM`, in WAL, base backups, replicas, `pg_dump` files, OS caches and freed disk blocks. None of these are inspected.
2. **One PostgreSQL database.** Files, object stores, caches outside the DB, search indexes, message queues, analytics warehouses and third-party processors are out of scope. Registered `EXTERNAL` artifacts (e.g. `external.crm_export_files`) are only *scheduled* (`SCHEDULE_EXTERNAL`) and can never be verified. Any subject reachable from one therefore ends **PARTIAL**, never VERIFIED. This is deliberate.
3. **Not legal compliance.** Retention policies and legal bases are operator inputs. LAE does not decide what GDPR Art. 17, the DPDP Act 2023 or any other law requires.

## 2. Discovery
4. **Lineage sources** are declared FKs, view/matview definitions (SQLGlot), registered key mappings, and human-confirmed inferred edges. Application code, triggers, stored procedures, ETL jobs and `INSERT … SELECT` history are not analysed.
5. **Inference is conservative.** At θ = 0.95 the containment-based inference did *not* suggest `marketing_event_log.email → customer.email` (containment 17/19 = 0.805 at tiny scale, because the hidden copies differ in case and whitespace). The lineage-independent **residual scan** finds these copies, and its hits are fed back as SUGGESTED edges for a human to confirm.
6. **Exact-match traversal.** A confirmed text edge (`customer.email → marketing_event_log.email`) deletes rows whose value equals the subject's value exactly. Case and whitespace variants survive and are reported by the residual scan, giving an honest FAILED outcome.
7. **SQLGlot coverage.** Recursive CTEs, unknown functions, `LATERAL` set-returning functions and dynamic SQL make an artifact `OPAQUE`, which gives an `UNRESOLVED` item (outcome at most PARTIAL). Without the service's `extra_keys`, `GROUP BY customer_id` matviews would be classified AGGREGATE instead of KEY_PRESERVING.
8. **The SHARED rule is conservative.** A subject's message on an anonymous or other subject's ticket is classified SHARED (never deleted) even where a human might call it owned.
9. **PK types:** int and text (UUID as text). Tables without a PK are matched by predicate, which gives weaker per-record evidence (one predicate leaf, `NO_PRIMARY_KEY` warning).
10. `suggest_pii` (heuristic PII suggestions) exists and is tested, but no API or CLI path calls it. PII classification is seeded and confirmed by the operator.

## 3. Planning and execution
11. **Aggregates are recomputed, not edited.** The subject's contribution to `SUM`/`COUNT` artifacts is removed by `REFRESH MATERIALIZED VIEW` / recompute. Cost grows with artifact size.
12. **RECOMPUTE_TABLE has no rowcount assertion.** A wrong definition is caught only afterwards by `DERIVED_CONSISTENT`.
13. **The pseudonym-vs-NULL choice** for anonymised columns is derived from policies at execution time and is not bound by the plan hash.
14. **Synchronous API.** Long operations block the HTTP request. Benchmarks use the Python API/CLI.
15. **Concurrency** is protected by a SERIALIZABLE transaction, a drift re-check, `FOR UPDATE` locks and a cascade-count assertion. Data the subject creates *after* commit needs a new request.

## 4. Verification
16. **The residual scan matches whole normalised values only** (EXACT, LOWER_TRIM = ASCII whitespace trim + ASCII lowercase, DIGITS_ONLY). Not detected: substrings in free text, Unicode case variants (`É` vs `é`), encodings (base64, URL-encoding), hashes, encrypted values, transformed or concatenated values, array columns.
17a. **Non-unique identifiers are not residual-scanned.** A probe whose value also occurs in another row of the subject-root table (e.g. two customers with the same name) is excluded at planning time, and the count is recorded as `ambiguous_probes_excluded`. Without this, another person's identical name made correct erasures FAILED (found in the live frontend run, see screenshot 05 and CR frontend-engineer-2). The cost is that a surviving copy of a *shared* name is not detected. Unique identifiers (email, phone) are still scanned.
17. With zero probes the residual scan passes vacuously. Hits inside a legally retained table are reported as `retained_hits` and do not fail the check (per-table granularity).
18. **Residual counts are matches, not distinct rows**, because one row can match several normalisations. Pass/fail is unaffected. Benchmarks label the number as matches.
19. **EMBEDDING_ABSENT is key-based.** The vector-digest comparison was deferred, so embeddings of the subject stored under unrelated keys are not detected. Information memorised in model weights is out of scope.

## 5. Audit and evidence
20. **A valid signature ≠ complete erasure.** See the trust model in [security_analysis.md §3](security_analysis.md). Always read `verification.outcome`.
21. **Tamper-evident, not tamper-proof.** A DB superuser can rewrite the whole chain consistently. This is detectable only against chain-head anchors stored by a third party. `lae audit export-head` writes them to the local `var/anchors` directory, which is not truly external.
22. **Purge only on VERIFIED.** PARTIAL and FAILED requests keep the raw subject key (needed for follow-up), with no retention deadline. `erasure_request.reason` and actor strings are never purged.
23. API `event_id` is derived from the event hash, not the stored `event_id` column.

## 6. Security and operations
24. **Local-prototype auth:** one shared token. `X-LAE-Actor` is a self-asserted claim (four-eyes compares claims). No per-user identity.
25. Dev-only passwords in compose/init. The signing key is unencrypted PEM. File mode 0600 is ineffective on Windows.
26. The compose port binding to `127.0.0.1` takes effect only after the DB container is recreated.

## 7. Evaluation validity
27. Synthetic data from one generator (the same team wrote discovery). The ground truth comes from the generator's own bookkeeping, not from FK queries, but its notion of "owned" follows the same design.
28. Laptop benchmarks (Windows host, Docker Desktop VM) are noisy. See [benchmarking.md](benchmarking.md) for repeat counts and the environment.
