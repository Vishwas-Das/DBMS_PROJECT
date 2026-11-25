# Benchmarking and Experimental Results

All numbers here were **measured** by `python -m benchmarks.run` (harness in `benchmarks/`) and copied from `benchmarks/results/summary.md`. That file is the authoritative output: it holds the full tables, the per-category and per-stratum breakdowns, and the environment. The raw per-subject rows (CSV/JSON) are in `benchmarks/results/raw/` (gitignored). Charts: `benchmarks/results/charts/*.svg`. Nothing is estimated, and no performance improvement is claimed beyond what the tables show.

## 1. Question
Does combining schema/FK discovery, SQL lineage, registered artifacts, record-level roles, policy-aware planning and an independent residual scan erase a subject's data more completely and more safely than (B1) a hand-written deletion script or (B2) FK-only discovery? What does it cost?

## 2. Setup
- **Data:** deterministic synthetic generator (`db/fixtures/generator.py`), seed 42. Scales: **small** = 100 customers, **default** = 1 000 (3 000 orders, 1 000 tickets, 500 documents, 500 embeddings), **large** = 10 000 (30 000 orders, 75 094 order lines). The large scale completed in full (wall time 1 218 s).
- **Subjects:** 20 per set, stratified by {hidden copies yes/no} × {shared records yes/no}, pairwise independent. Set S1 is used for every method. Sets S2_control / S2_after_feedback are used for the feedback experiment.
- **Fresh data per method:** before every method pass the bench DB (`lae_test_bench`) is dropped, migrated and re-seeded byte-identically.
- **Ground truth:** the generator's own bookkeeping, never derived from FKs. It covers owned rows, derived key-addressable rows (matview, CTAS, embedding, cache), and **hidden copies** (`marketing_event_log` rows holding the subject's email with case/whitespace variants, with no FK).
- **Methods:**
  - **B1 manual:** hard-coded DELETEs of the "obvious" tables in one transaction. It knows nothing about derived artifacts, caches, embeddings, hidden copies or retention.
  - **B2 FK-only:** LAE's FK discovery + record BFS, then delete every discovered SUBJECT_ROOT/OWNED/SHARED row child-first.
  - **LAE:** the full service pipeline: request → discovery → plan → acknowledge acknowledgeable conflicts → approve(hash) → LIVE (SERIALIZABLE, drift check) → verify → evidence.
- **Metrics:** precision/recall/F1 (micro-averaged, `lae.verification.metrics`, exact fractions). **Erasure coverage** is measured by direct SQL after execution, independently of the engine's own verdict. Residual rows come from an independent SQL scan (whole-value HMAC + substring). **Unrelated damage** uses per-row md5 before/after. Shared rows destroyed and retention violations are counted separately. Latency uses `time.perf_counter` per phase, p50/p95 by linear interpolation. DB overhead is `pg_total_relation_size` of schema `lae` plus row deltas.
- **Environment:** Windows 11 (10.0.26300), Intel Core 5 120U (10 C / 12 T), 15.6 GiB RAM. Python 3.12.10. PostgreSQL 16.15 + pgvector 0.8.7 + pgcrypto 1.3 in Docker 29.9.2 (VM: 12 CPUs, 7.6 GiB). shared_buffers 128 MB, work_mem 4 MB, fsync on. Warm runs (no cache drop).
- **Code version:** `99082c5` (after the namesake-probe fix). Later commits change only concurrent-execution serialisation, one HTTP status mapping, test isolation and the DB host name. None of these changes the sequential code path measured here.

## 3. Results: completeness and safety (set S1)

| scale | method | precision | recall | coverage | coverage (committed) | residual rows (before) | unrelated damage | shared rows destroyed | retention violations | committed / failed / blocked |
|---|---|---|---|---|---|---|---|---|---|---|
| small | B1 manual | 0.952 | 0.309 | 0.400 | 0.833 | 85 (86) | 0 | 0 | 0 | 1 / 19 / 0 |
| small | B2 FK-only | 1 | 0.813 | 0.315 | 0.315 | 57 (86) | 0 | **12** | **258** | 20 / 0 / 0 |
| small | **LAE** | 1 | 0.902 | **0.743** | 0.900 | 60 (86) | 0 | 0 | 0 | 14 / 0 / 6 |
| default | B1 manual | 0.959 | 0.251 | 0.460 | n/a | 83 (83) | 0 | 0 | 0 | 0 / 20 / 0 |
| default | B2 FK-only | 1 | 0.824 | 0.252 | 0.252 | 51 (83) | 0 | **10** | **322** | 20 / 0 / 0 |
| default | **LAE** | 1 | 0.920 | **0.751** | 0.898 | 60 (83) | 0 | 0 | 0 | 14 / 0 / 6 |
| large | B1 manual | 0.992 | 0.237 | 0.481 | n/a | 76 (76) | 0 | 0 | 0 | 0 / 20 / 0 |
| large | B2 FK-only | 1 | 0.837 | 0.239 | 0.239 | 48 (76) | 0 | **10** | **331** | 20 / 0 / 0 |
| large | **LAE** | 1 | 0.928 | **0.904** | 0.923 | 41 (76) | 0 | 0 | 0 | 19 / 0 / 1 |

Recall against the FK-reachable truth alone is 1.0 for both B2 and LAE at every scale.

**Reading the table**
1. **B1 (hard-coded DELETE) mostly does not run.** 59 of 60 subjects failed on a foreign-key violation (orders reference the customer with `ON DELETE RESTRICT`). Its apparent "retained" correctness comes from never touching orders. This is the concrete answer to *why `DELETE FROM customer WHERE id = X` is insufficient*.
2. **B2 (FK-only) deletes everything it can reach, and that is unsafe.** It destroyed 10–12 shared records (other customers' messages) and caused 258–331 retention violations (legally retained orders and lines deleted). It also leaves every derived artifact, cache, embedding and hidden copy untouched, so its coverage is the lowest (0.24–0.32).
3. **LAE has the highest coverage (0.74–0.90) with zero damage, zero shared rows destroyed and zero retention violations.** Its remaining gap has two measured causes:
   - **Blocked plans** (6/20 at small and default, 1/20 at large) for subjects whose shared records cannot be detached (NOT NULL link). The planner refuses to destroy another person's data, which is the design.
   - **Hidden copies** with no FK (hidden category 0/40–0/50 in S1) until a human confirms the residual-scan suggestion (§4).
   The engine's own outcomes were honest: FAILED when residual copies remained, PARTIAL otherwise (the external CRM artifact is never verifiable in-DB), and never a false VERIFIED.

## 4. Results: the residual-feedback loop

After LAE ran on S1, the SUGGESTED edges produced by the residual scan (`customer.email → marketing_event_log.email`, TABLE + COLUMN) were confirmed, and LAE ran on new subjects (S2_after_feedback). S2_control is the same subjects on fresh data without confirmation.

| scale | S2_control: coverage / residual rows / outcomes | S2_after_feedback: coverage / residual rows / outcomes | hidden copies erased |
|---|---|---|---|
| small | 0.866 / 62 / FAILED 19 | **0.977 / 4 / PARTIAL 19** | 58 / 61 |
| default | 0.749 / 50 / FAILED 9, PARTIAL 6 | **0.818 / 16 / PARTIAL 15** | 34 / 39 |
| large | 0.791 / 34 / FAILED 7, PARTIAL 7 | **0.827 / 14 / PARTIAL 14** | 20 / 24 |

One human confirmation of a suggestion produced by the **lineage-independent** scan removed 83–95 % of the hidden copies for later subjects. All remaining copies (3, 5 and 4 at the three scales) belong to subjects whose plan was BLOCKED, so nothing was executed for them. Every committed subject's copies were erased (58/58, 34/34, 20/20). PARTIAL comes only from the unverifiable external CRM artifact. Recall rose to 1.0 in every after-feedback set.

## 5. Results: latency (ms per subject, total p50 / p95)

| scale | B1 manual | B2 FK-only | LAE (S1) | LAE phase p50: discovery / planning / execution / verification |
|---|---|---|---|---|
| small | 30 / 49 | 137 / 172 | 1 250 / 4 169 | 122 / 259 / 238 / 351 |
| default | 28 / 39 | 156 / 250 | 1 712 / 2 029 | 110 / 302 / 255 / 832 |
| large | 22 / 32 | 151 / 240 | 7 511 / 18 336 | 152 / 948 / 371 / 5 694 |

At p50, LAE is about 9–50× slower than B2 and 42–340× slower than B1. The **verification** phase dominates at large scale, because the residual scan computes an HMAC over every text cell of every target table (O(total text cells)). The large p95 values (total 18.3 s, verification 25.4 s) are interpolations pulled up by one 192.8 s verification outlier. The next slowest verification took 6.9 s. Planning includes a second discovery pass (the pipeline re-discovers inside `generate_plan`). These costs are per erasure request, not per query of the application. Whether they are acceptable depends on request volume, which we did not measure.

## 6. Results: database overhead (LAE)

0.88–1.58 MB added to schema `lae` per set of 20 requests. 77–90 rows written to `lae` per request (plan items, record dependencies, checks, evidence). 7.2–8.7 audit events per request. Ambiguous (non-unique) residual probes were excluded for 4/20 (small), 17/20 (default) and 20/20 (large) subjects in S1. Larger populations contain more namesakes, which is the measured cost of the namesake fix (limitation 17a).

## 7. Threats to validity
- **One synthetic generator.** Its hard cases (RESTRICT orders, shared tickets, hidden copies) are designed. Real schemas will differ.
- **The ground truth's notion of "owned"** follows the same design as the planner, although it is computed independently.
- **20 subjects per set.** p95 is sensitive to single outliers (e.g. the 192.8 s verification).
- **Laptop timings** (Windows host, Docker VM, warm caches) are noisy and not comparable to server hardware.
- **Scales and baselines:** B1/B2 are our implementations of the baselines, not third-party tools.

## 8. Reproduce
```powershell
.\.venv\Scripts\python -m benchmarks.run --scales small,default,large --subjects 20 --seed 42 --db-url postgresql+psycopg://lae_owner:lae_owner_dev@127.0.0.1:5433/lae_test_bench
.\.venv\Scripts\python -m benchmarks.report      # regenerates summary.md / charts from results
```
(`--help` lists all options. The runner drops and re-seeds the given database, so never point it at `lae`.)
