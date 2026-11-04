"""Benchmark runner: three erasure methods x scales, measured independently by SQL.

    python -m benchmarks.run --scales small,default,large --subjects 20 --seed 42 \
        --db-url postgresql://lae_owner:...@127.0.0.1:5433/lae_test_bench

Protocol (documented in results/summary.md):

1. For each scale the generator ground truth is built in memory (deterministic seed) and 2N
   mutually independent subjects are picked, stratified by (hidden copies?, shared rows?):
   set S1 (main comparison) and set S2 (residual-feedback experiment).
2. For EACH method the database is reset (schemas dropped, ``alembic upgrade head``), re-seeded
   with the same scale/seed (byte-identical data) and ``VACUUM ANALYZE``-ed, so every method
   starts from identical fresh data. Subjects of a set run sequentially on that database.
   Because the subjects are independent (no shared rows / referrals between them), erasing
   one does not change another's ground truth.
3. Per subject: row snapshot (md5 per row) -> method -> row snapshot; coverage, damage, an
   independent HMAC residual scan and precision/recall are computed by direct SQL.
4. Residual feedback: after PROPOSED on S1, the residual-scan SUGGESTED edges are CONFIRMED and
   PROPOSED runs on S2 in the same database. Control: PROPOSED on S2 on fresh data without
   confirmation.

Raw per-subject rows go to results/raw/ (gitignored); summaries to results/summary.{json,csv,md}.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import secrets
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

from benchmarks import dbsetup, environment, measure, stats
from benchmarks.methods import METHODS, FkOnlyDeletion, LaePipeline, ManualDeletion

log = logging.getLogger("benchmarks.run")
RESULTS = Path(__file__).resolve().parent / "results"
DEFAULT_URL = "postgresql://lae_owner:lae_owner_dev@127.0.0.1:5433/lae_test_bench"
PHASES = (
    "request",
    "discovery",
    "planning",
    "acknowledge",
    "approval",
    "execution",
    "verification",
    "evidence",
    "total",
)


# ------------------------------------------------------------------ subject selection


def _neighbours(truth: Any) -> dict[int, set[int]]:
    nb: dict[int, set[int]] = {c: set() for c in truth.subjects}
    for c, v in truth.subjects.items():
        others = {int(o) for _t, _k, o in v["shared"]}
        others |= {int(k[0]) for _t, k in v["referencing_other"]}
        for o in others:
            nb[c].add(o)
            nb.setdefault(o, set()).add(c)
    return nb


def stratum(v: Mapping[str, Any]) -> str:
    return f"hidden={'Y' if v['hidden_copies'] else 'N'},shared={'Y' if v['shared'] else 'N'}"


def select_subjects(truth: Any, n: int, seed: int, sets: int = 2) -> list[list[int]]:
    """`sets` disjoint lists of n subjects, round-robin over strata, pairwise independent."""
    rng = random.Random(seed)  # noqa: S311 - deterministic sampling, not cryptography
    by: dict[str, list[int]] = {}
    for c in sorted(truth.subjects):
        by.setdefault(stratum(truth.subjects[c]), []).append(c)
    for k in sorted(by):
        rng.shuffle(by[k])
    nb = _neighbours(truth)
    chosen: set[int] = set()
    out: list[list[int]] = []
    for _ in range(sets):
        picked: list[int] = []
        cursors = dict.fromkeys(by, 0)
        while len(picked) < n:
            progressed = False
            for k in sorted(by):
                if len(picked) >= n:
                    break
                lst = by[k]
                while cursors[k] < len(lst):
                    c = lst[cursors[k]]
                    cursors[k] += 1
                    if c in chosen or nb.get(c, set()) & chosen:
                        continue
                    picked.append(c)
                    chosen.add(c)
                    progressed = True
                    break
            if not progressed:
                break  # population exhausted
        for k in by:
            by[k] = [c for c in by[k] if c not in chosen]
        out.append(picked)
    return out


# ------------------------------------------------------------------ per-subject measurement


def measure_subject(
    method: Any,
    url: str,
    customer_id: int,
    gt: Mapping[str, Any],
    columns: Mapping[str, list[str]],
    hmac_key: str,
) -> dict[str, Any]:
    truth = measure.SubjectTruth.from_truth(gt)
    with dbsetup.connect(url, autocommit=True) as conn:
        ident = measure.load_identity(conn, customer_id)
        before = measure.take_snapshot(
            conn, ident, truth.anonymize_units(), truth.referencing_other
        )
        lae_before = measure.lae_counts(conn)
        resid_before = measure.residual_scan(conn, ident, hmac_key, columns)
    res = method.run_subject(customer_id)
    with dbsetup.connect(url, autocommit=True) as conn:
        after = measure.take_snapshot(conn, ident, (), ())
        lae_after = measure.lae_counts(conn)
        coverage = measure.erasure_coverage(conn, ident, truth, before, after)
        dmg = measure.damage(truth, ident, before, after)
        resid = measure.residual_scan(conn, ident, hmac_key, columns)
    quality = measure.discovery_quality(res.discovered, truth, before)
    lae_delta = {t: lae_after.get(t, 0) - lae_before.get(t, 0) for t in sorted(lae_after)}
    return {
        "subject": customer_id,  # a synthetic customer id from the generator (not PII)
        "stratum": stratum(gt),
        "status": res.status,
        "error": res.error,
        "timings_ms": {k: round(v, 3) for k, v in res.timings_ms.items()},
        "quality": quality,
        "coverage": coverage,
        "damage": dmg,
        "residual_before": {k: resid_before[k] for k in ("hmac_rows", "substring_rows")},
        "residual": resid,
        "lae_rows_added": sum(lae_delta.values()),
        "audit_events_added": lae_delta.get("audit_event", 0),
        "lae_rows_by_table": {k: v for k, v in lae_delta.items() if v},
        "details": res.details,
    }


# ------------------------------------------------------------------ aggregation


def _frac(num: int, den: int) -> dict[str, Any] | None:
    if den == 0:
        return None
    f = Fraction(num, den)
    return {"exact": f"{f.numerator}/{f.denominator}", "value": round(float(f), 6)}


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    tp = sum(r["quality"]["tp"] for r in rows)
    fp = sum(r["quality"]["fp"] for r in rows)
    fn = sum(r["quality"]["fn"] for r in rows)
    fk_tp = sum(r["quality"]["fk_tp"] for r in rows)
    fk_fn = sum(r["quality"]["fk_fn"] for r in rows)
    from lae.verification import metrics

    p, r_ = metrics.precision(tp, fp), metrics.recall(tp, fn)
    f1 = metrics.f1(p, r_)
    correct = sum(r["coverage"]["units_correct"] for r in rows)
    total = sum(r["coverage"]["units_total"] for r in rows)
    cats: dict[str, list[int]] = {}
    for r in rows:
        for k, v in r["coverage"]["by_category"].items():
            c = cats.setdefault(k, [0, 0])
            c[0] += v["correct"]
            c[1] += v["total"]
    statuses: dict[str, int] = {}
    outcomes: dict[str, int] = {}
    for r in rows:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
        o = r["details"].get("outcome")
        if o:
            outcomes[o] = outcomes.get(o, 0) + 1
    lat = {
        ph: stats.describe([r["timings_ms"][ph] for r in rows if ph in r["timings_ms"]])
        for ph in PHASES
        if any(ph in r["timings_ms"] for r in rows)
    }
    by_stratum: dict[str, dict[str, Any]] = {}
    for r in rows:
        s = by_stratum.setdefault(r["stratum"], {"subjects": 0, "correct": 0, "total": 0})
        s["subjects"] += 1
        s["correct"] += r["coverage"]["units_correct"]
        s["total"] += r["coverage"]["units_total"]
    for s in by_stratum.values():
        s["coverage"] = _frac(s["correct"], s["total"])

    def fv(f: Fraction | None) -> dict[str, Any] | None:
        return None if f is None else _frac(f.numerator, f.denominator)

    return {
        "subjects": len(rows),
        "status_counts": statuses,
        "lae_outcomes": outcomes,
        "discovery": {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": fv(p),
            "recall": fv(r_),
            "f1": fv(f1),
            "recall_fk_truth": _frac(fk_tp, fk_tp + fk_fn),
        },
        "coverage": _frac(correct, total),
        "coverage_committed_only": _frac(
            sum(r["coverage"]["units_correct"] for r in rows if r["status"] == "COMMITTED"),
            sum(r["coverage"]["units_total"] for r in rows if r["status"] == "COMMITTED"),
        ),
        "coverage_units": {"correct": correct, "total": total},
        "coverage_by_category": {
            k: ({**(_frac(*v) or {}), "correct": v[0], "total": v[1]} if v[1] else None)
            for k, v in sorted(cats.items())
        },
        "coverage_by_stratum": dict(sorted(by_stratum.items())),
        "residual_hmac_rows": sum(r["residual"]["hmac_rows"] for r in rows),
        "residual_substring_rows": sum(r["residual"]["substring_rows"] for r in rows),
        "residual_hmac_rows_before": sum(r["residual_before"]["hmac_rows"] for r in rows),
        "residual_substring_rows_before": sum(r["residual_before"]["substring_rows"] for r in rows),
        "subjects_with_zero_residual": sum(1 for r in rows if r["residual"]["substring_rows"] == 0),
        "unrelated_damage": sum(r["damage"]["unrelated_damage"] for r in rows),
        "other_customers_deleted": sum(r["damage"]["other_customers_deleted"] for r in rows),
        "shared_rows_deleted": sum(r["damage"]["shared_rows_deleted"] for r in rows),
        "shared_rows_modified": sum(r["damage"]["shared_rows_modified"] for r in rows),
        "retention_violations": sum(r["coverage"]["retention_violations"] for r in rows),
        "hidden_truth_mismatches": sum(
            1 for r in rows if not r["coverage"]["hidden_before_matches_truth"]
        ),
        "residual_probes_total": sum(r["details"].get("residual_probes") or 0 for r in rows),
        "ambiguous_probes_excluded_total": sum(
            r["details"].get("ambiguous_probes_excluded") or 0 for r in rows
        ),
        "subjects_with_ambiguous_probes": sum(
            1 for r in rows if (r["details"].get("ambiguous_probes_excluded") or 0) > 0
        ),
        "latency_ms": lat,
        "lae_rows_added_per_request": stats.describe([r["lae_rows_added"] for r in rows]),
        "audit_events_per_request": stats.describe([r["audit_events_added"] for r in rows]),
    }


def flat_metrics(s: Mapping[str, Any], run: Mapping[str, Any]) -> dict[str, Any]:
    """Top-level numeric metrics (the UI charts these)."""

    def v(x: Mapping[str, Any] | None) -> float | None:
        return None if x is None else x["value"]

    out: dict[str, Any] = {
        "subjects": s["subjects"],
        "committed": s["status_counts"].get("COMMITTED", 0),
        "failed": s["status_counts"].get("FAILED", 0),
        "blocked": s["status_counts"].get("BLOCKED", 0),
        "precision": v(s["discovery"]["precision"]),
        "recall": v(s["discovery"]["recall"]),
        "f1": v(s["discovery"]["f1"]),
        "recall_fk_truth": v(s["discovery"]["recall_fk_truth"]),
        "erasure_coverage": v(s["coverage"]),
        "erasure_coverage_committed": v(s.get("coverage_committed_only")),
        "residual_hmac_rows": s["residual_hmac_rows"],
        "residual_substring_rows": s["residual_substring_rows"],
        "unrelated_damage": s["unrelated_damage"],
        "shared_rows_deleted": s["shared_rows_deleted"],
        "retention_violations": s["retention_violations"],
        "lae_schema_bytes_delta": run.get("lae_size_delta_bytes"),
        "audit_events_per_request_mean": s["audit_events_per_request"]["mean"],
        "ambiguous_probes_excluded": s.get("ambiguous_probes_excluded_total"),
    }
    for ph, d in s["latency_ms"].items():
        out[f"{ph}_p50_ms"] = d["p50"]
        out[f"{ph}_p95_ms"] = d["p95"]
    for k, n in s["lae_outcomes"].items():
        out[f"outcome_{k.lower()}"] = n
    return out


# ------------------------------------------------------------------ orchestration


def fresh_database(url: str, scale: str, seed: int) -> tuple[Any, dict[str, float]]:
    t: dict[str, float] = {}
    t0 = time.perf_counter()
    dbsetup.reset_and_migrate(url)
    t["migrate_ms"] = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    truth = dbsetup.seed(url, scale, seed)
    t["seed_ms"] = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    dbsetup.vacuum_analyze(url)
    t["vacuum_analyze_ms"] = (time.perf_counter() - t0) * 1000
    return truth, {k: round(v, 1) for k, v in t.items()}


def run_method_pass(
    method_name: str,
    url: str,
    scale: str,
    seed: int,
    subject_sets: list[tuple[str, list[int]]],
    *,
    feedback: bool,
) -> list[dict[str, Any]]:
    """Fresh data, then each (set_name, subjects) in order on the same database."""
    truth, setup = fresh_database(url, scale, seed)
    hmac_key = secrets.token_hex(32)
    with dbsetup.connect(url, autocommit=True) as conn:
        columns = measure.text_columns(conn)
        size_before = measure.lae_size_bytes(conn)
    method = METHODS[method_name](url)
    runs: list[dict[str, Any]] = []
    try:
        prep = method.prepare()
        for i, (set_name, subjects) in enumerate(subject_sets):
            confirmed = 0
            feedback_edges: list[str] = []
            if i > 0 and feedback and isinstance(method, LaePipeline):
                hit_cols = {
                    (str(h["table"]), str(h["column"]))
                    for prev in runs
                    for r in prev["rows"]
                    for h in r["details"].get("lae_residual_hits") or []
                }
                sugg = method.residual_suggestions(hit_cols)
                confirmed = method.confirm_edges(sugg)
                feedback_edges = sorted(
                    f"{e.level.value}:{e.source.qualified()}.{','.join(e.source_columns)}->"
                    f"{e.target.qualified()}.{','.join(e.target_columns)}"
                    for e in sugg
                )
                log.info("feedback: confirmed %d residual-scan suggested edges", confirmed)
            t0 = time.perf_counter()
            rows = []
            for n, c in enumerate(subjects, 1):
                row = measure_subject(method, url, c, truth.subject(c), columns, hmac_key)
                rows.append(row)
                log.info(
                    "%s %s %s [%d/%d] status=%s coverage=%d/%d units total=%.0fms",
                    scale, method_name, set_name, n, len(subjects), row["status"],
                    row["coverage"]["units_correct"], row["coverage"]["units_total"],
                    row["timings_ms"].get("total", 0.0),
                )  # fmt: skip
            wall = (time.perf_counter() - t0) * 1000
            with dbsetup.connect(url, autocommit=True) as conn:
                size_after = measure.lae_size_bytes(conn)
            runs.append(
                {
                    "scale": scale,
                    "method": method_name,
                    "set": set_name,
                    "subjects": subjects,
                    "setup_ms": setup,
                    "prepare_ms": {k: round(v, 1) for k, v in prep.items()},
                    "wall_ms": round(wall, 1),
                    "confirmed_feedback_edges": confirmed,
                    "confirmed_feedback_edge_list": feedback_edges,
                    "lae_size_before_bytes": size_before,
                    "lae_size_after_bytes": size_after,
                    "lae_size_delta_bytes": size_after - size_before,
                    "row_counts": truth.meta["row_counts"],
                    "rows": rows,
                }
            )
            size_before = size_after
    finally:
        close = getattr(method, "close", None)
        if close:
            close()
    return runs


def write_raw(runs: Iterable[Mapping[str, Any]], stamp: str, raw_dir: Path) -> list[Path]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    runs = list(runs)
    paths = []
    jpath = raw_dir / f"{stamp}_raw.json"
    jpath.write_text(json.dumps(runs, indent=1, sort_keys=True, default=str), encoding="utf-8")
    paths.append(jpath)
    cpath = raw_dir / f"{stamp}_raw.csv"
    fields = [
        "scale", "method", "set", "subject", "stratum", "status", "error",
        "precision", "recall", "f1", "recall_fk_truth", "tp", "fp", "fn",
        "coverage", "units_correct", "units_total", "retention_violations",
        "residual_hmac_rows", "residual_substring_rows", "unrelated_damage",
        "shared_rows_deleted", "other_customers_deleted", "lae_rows_added",
        "audit_events_added", "outcome", "residual_probes", "ambiguous_probes_excluded",
        *[f"{p}_ms" for p in PHASES],
    ]  # fmt: skip
    with cpath.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for run in runs:
            for r in run["rows"]:
                q, cov = r["quality"], r["coverage"]

                def val(x: Mapping[str, Any] | None) -> Any:
                    return "" if x is None else x["value"]

                w.writerow(
                    {
                        "scale": run["scale"],
                        "method": run["method"],
                        "set": run["set"],
                        "subject": r["subject"],
                        "stratum": r["stratum"],
                        "status": r["status"],
                        "error": r["error"] or "",
                        "precision": val(q["precision"]),
                        "recall": val(q["recall"]),
                        "f1": val(q["f1"]),
                        "recall_fk_truth": val(q["recall_fk_truth"]),
                        "tp": q["tp"],
                        "fp": q["fp"],
                        "fn": q["fn"],
                        "coverage": val(cov["coverage"]),
                        "units_correct": cov["units_correct"],
                        "units_total": cov["units_total"],
                        "retention_violations": cov["retention_violations"],
                        "residual_hmac_rows": r["residual"]["hmac_rows"],
                        "residual_substring_rows": r["residual"]["substring_rows"],
                        "unrelated_damage": r["damage"]["unrelated_damage"],
                        "shared_rows_deleted": r["damage"]["shared_rows_deleted"],
                        "other_customers_deleted": r["damage"]["other_customers_deleted"],
                        "lae_rows_added": r["lae_rows_added"],
                        "audit_events_added": r["audit_events_added"],
                        "outcome": r["details"].get("outcome", ""),
                        "residual_probes": r["details"].get("residual_probes", ""),
                        "ambiguous_probes_excluded": r["details"].get(
                            "ambiguous_probes_excluded", ""
                        ),
                        **{f"{p}_ms": r["timings_ms"].get(p, "") for p in PHASES},
                    }
                )
    paths.append(cpath)
    return paths


def store_in_dev_db(
    summary_runs: Sequence[Mapping[str, Any]],
    env: Mapping[str, Any],
    seed: int,
    url: str,
) -> int:
    """Insert one lae.benchmark_run row per (scale, method, set) into the DEV database `lae`."""
    from lae.db.repositories import BenchmarkRepo
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    if dbsetup.database_name(url) != "lae":
        raise SystemExit("--store-url must point at the dev database named 'lae'")
    engine = create_engine(dbsetup.sa_url(url), future=True, hide_parameters=True)
    try:
        with Session(engine) as s:
            for run in summary_runs:
                BenchmarkRepo(s).add(
                    run["scenario"],
                    params=run["params"],
                    seed=seed,
                    environment=dict(env),
                    metrics=run["metrics"],
                    git_commit=env.get("git_commit"),
                )
            s.commit()
    finally:
        engine.dispose()
    return len(summary_runs)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--scales", default="small,default,large")
    ap.add_argument("--subjects", type=int, default=20)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--db-url", default=DEFAULT_URL, help="a dedicated lae_test_<suffix> database")
    ap.add_argument("--methods", default=",".join(METHODS))
    ap.add_argument("--no-feedback", action="store_true", help="skip the residual-feedback sets")
    ap.add_argument("--no-control", action="store_true", help="skip the S2 control pass")
    ap.add_argument("--store-url", default=None, help="dev DB URL (database 'lae') for UI rows")
    ap.add_argument("--no-store", action="store_true", help="do not write lae.benchmark_run")
    ap.add_argument(
        "--merge", action="store_true", help="keep other (scale, method, set) runs in summary.json"
    )
    ap.add_argument("--store-sets", default=None, help="only store these sets (comma list)")
    ap.add_argument("--out", type=Path, default=RESULTS)
    ap.add_argument(
        "--resummarize",
        action="store_true",
        help="recompute summaries in summary.json from its raw files (no benchmark run)",
    )
    args = ap.parse_args(argv)
    if args.resummarize:
        return resummarize(args.out)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("lae", "sqlglot", "alembic", "lae.fixtures.generator"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    dbsetup.guard_bench_url(args.db_url)
    scales = [s.strip() for s in args.scales.split(",") if s.strip()]
    methods = [m.strip() for m in args.methods.split(",") if m.strip()]
    unknown = [m for m in methods if m not in METHODS]
    if unknown:
        ap.error(f"unknown methods {unknown}; choose from {list(METHODS)}")
    from db.fixtures import generator

    started = datetime.now(UTC)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    dbsetup.reset_and_migrate(args.db_url)
    env = environment.capture(args.db_url)
    all_runs: list[dict[str, Any]] = []
    scale_meta: dict[str, Any] = {}
    for scale in scales:
        t_scale = time.perf_counter()
        _ds, truth = generator.generate(scale, args.seed)
        s1, s2 = select_subjects(truth, args.subjects, args.seed)
        scale_meta[scale] = {
            "scale_params": asdict(generator.resolve_scale(scale)[1]),
            "row_counts": truth.meta["row_counts"],
            "S1": s1,
            "S2": s2,
            "S1_strata": _strata_counts(truth, s1),
            "S2_strata": _strata_counts(truth, s2),
        }
        log.info("scale %s: S1=%d subjects, S2=%d subjects", scale, len(s1), len(s2))
        for m in methods:
            sets: list[tuple[str, list[int]]] = [("S1", s1)]
            fb = m == LaePipeline.name and not args.no_feedback and bool(s2)
            if fb:
                sets.append(("S2_after_feedback", s2))
            all_runs += run_method_pass(m, args.db_url, scale, args.seed, sets, feedback=fb)
            if fb and not args.no_control:
                all_runs += run_method_pass(
                    m,
                    args.db_url,
                    scale,
                    args.seed,
                    [("S2_control", s2)],
                    feedback=False,
                )
        scale_meta[scale]["wall_s"] = round(time.perf_counter() - t_scale, 1)
        log.info("scale %s finished in %.1fs", scale, scale_meta[scale]["wall_s"])

    finished = datetime.now(UTC)
    raw_paths = write_raw(all_runs, stamp, args.out / "raw")
    summary_runs = []
    for run in all_runs:
        s = summarize(run["rows"])
        summary_runs.append(
            {
                "scenario": "B5",
                "scale": run["scale"],
                "method": run["method"],
                "set": run["set"],
                "params": {
                    "scale": run["scale"],
                    "method": run["method"],
                    "set": run["set"],
                    "subjects": len(run["subjects"]),
                    "seed": args.seed,
                    "row_counts": run["row_counts"],
                },
                "setup_ms": run["setup_ms"],
                "prepare_ms": run["prepare_ms"],
                "wall_ms": run["wall_ms"],
                "confirmed_feedback_edges": run["confirmed_feedback_edges"],
                "confirmed_feedback_edge_list": run.get("confirmed_feedback_edge_list", []),
                "lae_size_before_bytes": run["lae_size_before_bytes"],
                "lae_size_after_bytes": run["lae_size_after_bytes"],
                "lae_size_delta_bytes": run["lae_size_delta_bytes"],
                "summary": s,
                "metrics": flat_metrics(s, run),
            }
        )
    out = {
        "generated_at": finished.isoformat(timespec="seconds"),
        "started_at": started.isoformat(timespec="seconds"),
        "duration_s": round((finished - started).total_seconds(), 1),
        "seed": args.seed,
        "subjects_per_set": args.subjects,
        "methods": {
            ManualDeletion.name: ManualDeletion.description,
            FkOnlyDeletion.name: FkOnlyDeletion.description,
            LaePipeline.name: LaePipeline.description,
        },
        "scales": scale_meta,
        "environment": env,
        "runs": summary_runs,
        "raw_files": [p.name for p in raw_paths],
    }
    summary_path = args.out / "summary.json"
    if args.merge and summary_path.exists():
        old = json.loads(summary_path.read_text(encoding="utf-8"))
        produced = {(r["scale"], r["method"], r["set"]) for r in summary_runs}
        keep = [
            r for r in old.get("runs", []) if (r["scale"], r["method"], r["set"]) not in produced
        ]
        out["runs"] = keep + summary_runs
        out["scales"] = dict(old.get("scales", {}))
        for k, v in scale_meta.items():  # keep the wall time of the original full pass
            prev = out["scales"].get(k, {})
            out["scales"][k] = {**prev, **v, "wall_s": prev.get("wall_s", v.get("wall_s"))}
        out["partial_reruns"] = [
            *old.get("partial_reruns", []),
            {
                "generated_at": out["generated_at"],
                "git_commit": env.get("git_commit"),
                "runs": sorted("/".join(k) for k in produced),
            },
        ]
        out["environment"] = old.get("environment", env)
        out["started_at"], out["duration_s"] = old["started_at"], old["duration_s"]
        out["generated_at"] = old["generated_at"]
        out["merged_from"] = sorted(
            set(old.get("merged_from", [old.get("generated_at")])) | {out["generated_at"]}
        )
    args.out.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(out, indent=2, sort_keys=True), encoding="utf-8")
    from benchmarks import report

    report.render(summary_path, args.out)
    if not args.no_store:
        from lae.config import Settings

        store_url = args.store_url or Settings().database_url
        to_store = summary_runs
        if args.store_sets:
            wanted = {x.strip() for x in args.store_sets.split(",")}
            to_store = [r for r in summary_runs if r["set"] in wanted]
        n = store_in_dev_db(to_store, env, args.seed, store_url)
        log.info("stored %d benchmark_run rows in the dev database", n)
    log.info("summary written to %s (raw: %s)", summary_path, ", ".join(map(str, raw_paths)))
    return 0


def resummarize(out_dir: Path) -> int:
    """Rebuild every run's `summary`/`metrics` from the raw per-subject rows (pure, no DB)."""
    path = out_dir / "summary.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    raw: dict[tuple[str, str, str], dict[str, Any]] = {}
    for f in sorted((out_dir / "raw").glob("*_raw.json")):
        for run in json.loads(f.read_text(encoding="utf-8")):
            raw[(run["scale"], run["method"], run["set"])] = run  # newest file wins
    for r in data["runs"]:
        run = raw.get((r["scale"], r["method"], r["set"]))
        if run is None:
            raise SystemExit(f"raw rows missing for {r['scale']}/{r['method']}/{r['set']}")
        r["summary"] = summarize(run["rows"])
        r["metrics"] = flat_metrics(r["summary"], run)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    from benchmarks import report

    report.render(path, out_dir)
    log.info("re-summarized %d runs from raw rows", len(data["runs"]))
    return 0


def _strata_counts(truth: Any, subjects: Iterable[int]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in subjects:
        k = stratum(truth.subjects[c])
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


if __name__ == "__main__":
    sys.exit(main())
