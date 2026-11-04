"""Render results/summary.json into summary.md, summary.csv and hand-written SVG charts.

    python -m benchmarks.report [--summary benchmarks/results/summary.json]

No plotting library: charts are plain SVG strings (grouped bar charts).
"""

from __future__ import annotations

import argparse
import csv
import json
from collections.abc import Mapping, Sequence
from html import escape
from pathlib import Path
from typing import Any

RESULTS = Path(__file__).resolve().parent / "results"
PALETTE = ("#8c8c8c", "#4e79a7", "#e15759", "#59a14f", "#f28e2b", "#76b7b2")
SCALE_ORDER = ("tiny", "small", "default", "large")
SHORT = {
    "BASELINE_1_MANUAL": "B1 manual",
    "BASELINE_2_FK_ONLY": "B2 FK-only",
    "PROPOSED_LAE": "LAE",
}


def label(run: Mapping[str, Any]) -> str:
    base = SHORT.get(run["method"], run["method"])
    return base if run["set"] == "S1" else f"{base} [{run['set']}]"


def _num(x: Any) -> float | None:
    if isinstance(x, dict):
        return x.get("value")
    return float(x) if isinstance(x, int | float) else None


# ------------------------------------------------------------------ SVG


def bar_chart(
    title: str,
    groups: Sequence[str],
    series: Sequence[tuple[str, Sequence[float | None]]],
    *,
    unit: str = "",
    y_max: float | None = None,
    width: int = 760,
    height: int = 340,
) -> str:
    """Grouped vertical bars: one group per x label, one bar per series."""
    left, right, top, bottom = 70, 20, 40, 80
    pw, ph = width - left - right, height - top - bottom
    vals = [v for _n, vs in series for v in vs if v is not None]
    vmax = y_max if y_max is not None else (max(vals) if vals else 1.0)
    vmax = vmax or 1.0
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="sans-serif" font-size="11">',
        f'<rect width="{width}" height="{height}" fill="white"/>',
        f'<text x="{width / 2}" y="20" text-anchor="middle" font-size="14" '
        f'font-weight="bold">{escape(title)}</text>',
    ]
    for i in range(6):
        y = top + ph - ph * i / 5
        v = vmax * i / 5
        out.append(
            f'<line x1="{left}" x2="{left + pw}" y1="{y:.1f}" y2="{y:.1f}" stroke="#e5e5e5"/>'
        )
        out.append(
            f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{_fmt(v)}{escape(unit)}</text>'
        )
    out.append(
        f'<line x1="{left}" x2="{left}" y1="{top}" y2="{top + ph}" stroke="#333"/>'
        f'<line x1="{left}" x2="{left + pw}" y1="{top + ph}" y2="{top + ph}" stroke="#333"/>'
    )
    n_g, n_s = max(len(groups), 1), max(len(series), 1)
    gw = pw / n_g
    bw = gw * 0.8 / n_s
    for gi, g in enumerate(groups):
        gx = left + gi * gw + gw * 0.1
        for si, (_name, vs) in enumerate(series):
            v = vs[gi] if gi < len(vs) else None
            x = gx + si * bw
            if v is None:
                out.append(
                    f'<text x="{x + bw / 2:.1f}" y="{top + ph - 4}" text-anchor="middle" '
                    f'fill="#999">n/a</text>'
                )
                continue
            h = ph * min(v, vmax) / vmax
            out.append(
                f'<rect x="{x:.1f}" y="{top + ph - h:.1f}" width="{bw * 0.92:.1f}" '
                f'height="{h:.1f}" fill="{PALETTE[si % len(PALETTE)]}"><title>'
                f"{escape(_name)} / {escape(g)}: {_fmt(v)}{escape(unit)}</title></rect>"
            )
            out.append(
                f'<text x="{x + bw * 0.46:.1f}" y="{top + ph - h - 3:.1f}" text-anchor="middle" '
                f'font-size="9">{_fmt(v)}</text>'
            )
        out.append(
            f'<text x="{gx + gw * 0.4:.1f}" y="{top + ph + 16}" text-anchor="middle">'
            f"{escape(g)}</text>"
        )
    lx = left
    for si, (name, _vs) in enumerate(series):
        y = height - 30 + (si // 4) * 14
        x = lx + (si % 4) * 170
        out.append(
            f'<rect x="{x}" y="{y - 9}" width="10" height="10" '
            f'fill="{PALETTE[si % len(PALETTE)]}"/><text x="{x + 14}" y="{y}">{escape(name)}</text>'
        )
    out.append("</svg>")
    return "\n".join(out)


def _fmt(v: float) -> str:
    if v == 0:
        return "0"
    if abs(v) >= 100:
        return f"{v:.0f}"
    if abs(v) >= 1:
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{v:.3f}".rstrip("0").rstrip(".")


# ------------------------------------------------------------------ tables


def _scales(runs: Sequence[Mapping[str, Any]]) -> list[str]:
    present = {r["scale"] for r in runs}
    return [s for s in SCALE_ORDER if s in present] + sorted(present - set(SCALE_ORDER))


def summary_rows(runs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for r in runs:
        s = r["summary"]
        lat = s["latency_ms"]
        rows.append(
            {
                "scale": r["scale"],
                "method": r["method"],
                "set": r["set"],
                "subjects": s["subjects"],
                "committed": s["status_counts"].get("COMMITTED", 0),
                "failed": s["status_counts"].get("FAILED", 0),
                "blocked": s["status_counts"].get("BLOCKED", 0),
                "lae_outcomes": ";".join(f"{k}={v}" for k, v in sorted(s["lae_outcomes"].items())),
                "precision": _num(s["discovery"]["precision"]),
                "recall": _num(s["discovery"]["recall"]),
                "f1": _num(s["discovery"]["f1"]),
                "recall_fk_truth": _num(s["discovery"]["recall_fk_truth"]),
                "erasure_coverage": _num(s["coverage"]),
                "coverage_exact": (s["coverage"] or {}).get("exact"),
                "erasure_coverage_committed": _num(s.get("coverage_committed_only")),
                "residual_hmac_rows": s["residual_hmac_rows"],
                "residual_substring_rows": s["residual_substring_rows"],
                "residual_substring_rows_before": s["residual_substring_rows_before"],
                "unrelated_damage": s["unrelated_damage"],
                "shared_rows_deleted": s["shared_rows_deleted"],
                "retention_violations": s["retention_violations"],
                "discovery_p50_ms": (lat.get("discovery") or {}).get("p50"),
                "discovery_p95_ms": (lat.get("discovery") or {}).get("p95"),
                "planning_p50_ms": (lat.get("planning") or {}).get("p50"),
                "planning_p95_ms": (lat.get("planning") or {}).get("p95"),
                "execution_p50_ms": (lat.get("execution") or {}).get("p50"),
                "execution_p95_ms": (lat.get("execution") or {}).get("p95"),
                "verification_p50_ms": (lat.get("verification") or {}).get("p50"),
                "verification_p95_ms": (lat.get("verification") or {}).get("p95"),
                "total_p50_ms": (lat.get("total") or {}).get("p50"),
                "total_p95_ms": (lat.get("total") or {}).get("p95"),
                "snapshot_ms": r["prepare_ms"].get("snapshot"),
                "lae_size_delta_bytes": r["lae_size_delta_bytes"],
                "audit_events_per_request": s["audit_events_per_request"]["mean"],
                "lae_rows_per_request": s["lae_rows_added_per_request"]["mean"],
                "confirmed_feedback_edges": r["confirmed_feedback_edges"],
                "feedback_edge_list": "; ".join(r.get("confirmed_feedback_edge_list") or []),
                "residual_probes": s.get("residual_probes_total"),
                "ambiguous_probes_excluded": s.get("ambiguous_probes_excluded_total"),
                "subjects_with_ambiguous_probes": s.get("subjects_with_ambiguous_probes"),
            }
        )
    return rows


def _cell(v: Any) -> str:
    if v is None or v == "":
        return "n/a"
    if isinstance(v, float):
        return _fmt(v) if abs(v) >= 1 or v == 0 else f"{v:.4f}"
    return str(v)


def md_table(rows: Sequence[Mapping[str, Any]], cols: Sequence[tuple[str, str]]) -> str:
    head = "| " + " | ".join(h for _k, h in cols) + " |"
    sep = "|" + "|".join("---" for _ in cols) + "|"
    body = ["| " + " | ".join(_cell(r.get(k)) for k, _h in cols) + " |" for r in rows]
    return "\n".join([head, sep, *body])


def render(summary_path: Path, out_dir: Path) -> list[Path]:
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    runs = sorted(data["runs"], key=_run_key)
    rows = summary_rows(runs)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    csv_path = out_dir / "summary.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["scale"])
        w.writeheader()
        w.writerows(rows)
    written.append(csv_path)

    charts = out_dir / "charts"
    charts.mkdir(exist_ok=True)
    scales = _scales(runs)
    main = [r for r in runs if r["set"] == "S1"]
    labels = sorted({label(r) for r in main}, key=_method_rank)

    def series(metric: str, pool: Sequence[Mapping[str, Any]]) -> list[tuple[str, list]]:
        out = []
        for lab in sorted({label(r) for r in pool}, key=_method_rank):
            vals = []
            for sc in scales:
                hit = [r for r in pool if r["scale"] == sc and label(r) == lab]
                vals.append(hit[0]["metrics"].get(metric) if hit else None)
            out.append((lab, vals))
        return out

    specs = [
        ("coverage.svg", "Erasure coverage (direct SQL, S1)", "erasure_coverage", main, 1.0, ""),
        ("recall.svg", "Discovery recall vs full ground truth (S1)", "recall", main, 1.0, ""),
        ("precision.svg", "Discovery precision (S1)", "precision", main, 1.0, ""),
        ("residual.svg", "Residual rows holding the subject's email/phone after erasure (S1)",
         "residual_substring_rows", main, None, ""),
        ("total_latency_p50.svg", "Total latency per subject, p50 (S1)", "total_p50_ms", main,
         None, " ms"),
        ("total_latency_p95.svg", "Total latency per subject, p95 (S1)", "total_p95_ms", main,
         None, " ms"),
        ("feedback_coverage.svg", "Residual feedback: LAE coverage on S2 (control vs confirmed)",
         "erasure_coverage", [r for r in runs if r["method"] == "PROPOSED_LAE"], 1.0, ""),
        ("feedback_residual.svg", "Residual feedback: LAE residual rows",
         "residual_substring_rows", [r for r in runs if r["method"] == "PROPOSED_LAE"], None, ""),
    ]  # fmt: skip
    for fname, title, metric, pool, ymax, unit in specs:
        if not pool:
            continue
        p = charts / fname
        p.write_text(
            bar_chart(title, scales, series(metric, pool), unit=unit, y_max=ymax),
            "utf-8",
        )
        written.append(p)
    lae = [r for r in main if r["method"] == "PROPOSED_LAE"]
    if lae:
        phases = ("discovery", "planning", "execution", "verification", "evidence")
        ser = [(ph, [_phase(lae, sc, ph) for sc in scales]) for ph in phases]
        p = charts / "lae_phase_latency_p50.svg"
        p.write_text(
            bar_chart("LAE per-phase latency, p50 (S1)", scales, ser, unit=" ms"),
            "utf-8",
        )
        written.append(p)

    md = out_dir / "summary.md"
    svgs = [p.name for p in written if p.suffix == ".svg"]
    md.write_text(_markdown(data, rows, labels, svgs, runs), encoding="utf-8")
    written.append(md)
    return written


SET_ORDER = ("S1", "S2_control", "S2_after_feedback")


def _run_key(r: Mapping[str, Any]) -> tuple[int, int, int]:
    sc = SCALE_ORDER.index(r["scale"]) if r["scale"] in SCALE_ORDER else 99
    me = _method_rank(SHORT.get(r["method"], r["method"]))[0]
    st = SET_ORDER.index(r["set"]) if r["set"] in SET_ORDER else 9
    return (sc, me, st)


def _phase(runs: Sequence[Mapping[str, Any]], scale: str, phase: str) -> float | None:
    hit = [r for r in runs if r["scale"] == scale]
    if not hit:
        return None
    return (hit[0]["summary"]["latency_ms"].get(phase) or {}).get("p50")


def _method_rank(lab: str) -> tuple[int, str]:
    for i, k in enumerate(("B1", "B2", "LAE")):
        if lab.startswith(k):
            return (i, lab)
    return (9, lab)


def _gb(n: Any) -> str:
    return f"{n / 2**30:.1f} GiB" if isinstance(n, int) else "n/a"


def _markdown(
    data: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], labels: Sequence[str],
    charts: Sequence[str], runs: Sequence[Mapping[str, Any]] = (),
) -> str:  # fmt: skip
    env = data["environment"]
    pg = env.get("pg_settings", {})
    lines = [
        "# LAE benchmark results (B5 completeness + latency)",
        "",
        "Generated by `python -m benchmarks.run` (raw per-subject rows in `results/raw/`, "
        "gitignored). Every number below was measured. Nothing is estimated.",
        "",
        f"- generated: {data['generated_at']} (started {data['started_at']}, "
        f"{data['duration_s']} s wall)",
        f"- seed: {data['seed']}; subjects per set: {data['subjects_per_set']}",
        f"- git commit: `{env.get('git_commit')}` (dirty working tree: {env.get('git_dirty')})",
        *[
            f"- partial re-run {pr['generated_at']} at `{pr['git_commit'][:7]}`: "
            + ", ".join(pr["runs"])
            + " (fresh data; replaced the earlier rows for these keys)"
            for pr in data.get("partial_reruns", [])
        ],
        "",
        "## Environment",
        "",
        f"- OS: {env.get('os')}; CPU: {env.get('cpu_model')}; "
        f"{env.get('physical_cores')} physical / {env.get('logical_cores')} logical cores; "
        f"RAM {_gb(env.get('ram_bytes'))}",
        f"- Python {env.get('python')}; packages: "
        + ", ".join(f"{k} {v}" for k, v in sorted(env.get("packages", {}).items())),
        f"- PostgreSQL {env.get('postgres_version')} (`{env.get('postgres_version_full')}`), "
        f"pgvector {env.get('pgvector_version')}, pgcrypto {env.get('pgcrypto_version')}",
        f"- Docker server {env.get('docker_server_version')} ({env.get('docker_os')}, "
        f"{env.get('docker_ncpu')} CPUs, {_gb(env.get('docker_mem_bytes'))} for the VM); "
        "the database runs in the `pgvector/pgvector:pg16` container on the same host",
        "- PG settings: " + ", ".join(f"{k}={v}" for k, v in sorted(pg.items())),
        "- Caches were NOT dropped between subjects (warm runs). Each method pass starts with "
        "VACUUM ANALYZE on freshly seeded data.",
        "",
        "## Scales",
        "",
    ]
    for sc, meta in data["scales"].items():
        rc = meta["row_counts"]
        lines.append(
            f"- **{sc}**: {rc.get('customer')} customers, {rc.get('purchase_order')} orders, "
            f"{rc.get('order_line')} order lines, {rc.get('support_ticket')} tickets, "
            f"{rc.get('ticket_message')} messages, {rc.get('document')} documents, "
            f"{rc.get('marketing_event_log')} marketing events. S1 strata {meta['S1_strata']}; "
            f"S2 strata {meta['S2_strata']}. Scale wall time {meta.get('wall_s')} s."
        )
    lines += ["", "## Methods", ""]
    for k, v in data["methods"].items():
        lines.append(f"- **{k}**: {v}")
    lines += [
        "",
        "## Protocol and metric definitions",
        "",
        "- *Fresh data per method*: before every method pass the bench DB is reset (`app` and "
        "`lae` dropped, `alembic upgrade head`) and re-seeded with the same scale and seed "
        "(byte-identical rows). All methods see the same subjects (set S1), run sequentially. "
        "Subjects within a set are pairwise independent (no shared rows or referrals between "
        "them), so one erasure cannot change another subject's ground truth.",
        "- *Ground truth* is the generator's own bookkeeping (`db/fixtures/generator.py`), "
        "never derived from FKs. Discovery truth = owned rows + derived key-addressable rows "
        "(matview / CTAS / embedding / cache) + hidden copies (marketing_event_log rows whose "
        "lower/trim email equals the subject's). *FK-reachable truth* = owned rows only.",
        "- *Discovered* = rows the method identifies as the subject's to erase: B1 the rows "
        "matched by its hard-coded predicates; B2 and LAE the SUBJECT_ROOT/OWNED nodes of the "
        "record graph (predicate keys resolved to the rows they address). Precision/recall/F1 "
        "are micro-averaged (sums of tp/fp/fn over subjects) with `lae.verification.metrics` "
        "(exact fractions; floats only for display).",
        "- *Erasure coverage* (direct SQL after execution): per ground-truth unit, correct iff "
        "owned non-retained row deleted; ANONYMIZE row still present, unlinked from the subject "
        "and its PII columns changed or NULL; RETAIN row present and byte-identical; derived row "
        "gone; hidden copy gone; other customer's reference to the subject cleared.",
        "`coverage` counts every subject of the set (a BLOCKED or FAILED subject keeps its "
        "units, mostly incorrect); `coverage (committed only)` restricts to subjects whose "
        "erasure transaction committed.",
        "- *Residual rows*: an independent scan in SQL over every text/json column of every "
        "`app` table and matview: `hmac` = whole-value HMAC-SHA256 (pgcrypto) of lower/trim "
        "value equals the subject's email or phone; `substring` = the email occurs anywhere "
        "(free text, JSON), or the phone matches. Reported numbers are `substring` (superset).",
        "- *Unrelated damage*: rows deleted or modified that belong to neither the subject's "
        "owned/derived/shared rows nor the rows referencing the subject (md5 per row before/after; "
        "marketing log compared as a multiset). Must be 0. Shared rows destroyed and retention "
        "violations are counted separately.",
        "- *Latency*: `time.perf_counter` per phase per subject; p50/p95 by linear "
        "interpolation over the subjects of a set. LAE `planning` re-runs discovery inside "
        "`generate_plan` (that is how the pipeline works). Snapshot time is per pass.",
        "- *DB overhead*: `pg_total_relation_size` summed over schema `lae` before/after a set; "
        "rows added to `lae` tables (count(*) delta) and audit events per request.",
        "- *Residual feedback*: after LAE on S1, every SUGGESTED edge with evidence "
        "`residual_scan` is CONFIRMED (`pipeline.set_edge_status`), then LAE runs on S2 in the "
        "same DB (`S2_after_feedback`). `S2_control` = LAE on S2 on fresh data without "
        "confirmation. Blocked plans: acknowledgeable BLOCKING conflicts are acknowledged; "
        "non-acknowledgeable ones (shared records) leave the plan BLOCKED and nothing is executed.",
        "",
        "## Results",
        "",
    ]
    cols_q = [
        ("scale", "scale"), ("method", "method"), ("set", "set"), ("subjects", "n"),
        ("committed", "committed"), ("failed", "failed"), ("blocked", "blocked"),
        ("lae_outcomes", "LAE outcomes"), ("precision", "precision"), ("recall", "recall"),
        ("f1", "F1"), ("recall_fk_truth", "recall (FK truth)"),
        ("erasure_coverage", "coverage"),
        ("erasure_coverage_committed", "coverage (committed only)"),
        ("residual_substring_rows", "residual rows"),
        ("residual_substring_rows_before", "rows before"),
        ("unrelated_damage", "unrelated damage"), ("shared_rows_deleted", "shared destroyed"),
        ("retention_violations", "retention violations"),
    ]  # fmt: skip
    cols_t = [
        ("scale", "scale"), ("method", "method"), ("set", "set"),
        ("discovery_p50_ms", "disc p50"), ("discovery_p95_ms", "disc p95"),
        ("planning_p50_ms", "plan p50"), ("planning_p95_ms", "plan p95"),
        ("execution_p50_ms", "exec p50"), ("execution_p95_ms", "exec p95"),
        ("verification_p50_ms", "verify p50"), ("verification_p95_ms", "verify p95"),
        ("total_p50_ms", "total p50"), ("total_p95_ms", "total p95"),
        ("snapshot_ms", "snapshot"),
    ]  # fmt: skip
    cols_o = [
        ("scale", "scale"), ("method", "method"), ("set", "set"),
        ("lae_size_delta_bytes", "lae bytes added"),
        ("lae_rows_per_request", "lae rows / request"),
        ("audit_events_per_request", "audit events / request"),
        ("confirmed_feedback_edges", "confirmed feedback edges"),
        ("feedback_edge_list", "edges confirmed"),
        ("residual_probes", "residual probes"),
        ("ambiguous_probes_excluded", "ambiguous probes excluded"),
        ("subjects_with_ambiguous_probes", "subjects w/ ambiguous probes"),
    ]  # fmt: skip
    lines += ["### Completeness and safety", "", md_table(rows, cols_q), ""]
    lines += ["### Latency (ms, per subject)", "", md_table(rows, cols_t), ""]
    lae_rows = [r for r in rows if r["method"] == "PROPOSED_LAE"]
    lines += ["### Database overhead (LAE)", "", md_table(lae_rows, cols_o), ""]
    lines += ["### Coverage by category (units correct / total)", ""]
    for r in runs:
        cats = r["summary"]["coverage_by_category"]
        txt = ", ".join(f"{k} {v.get('correct', '?')}/{v['total']}" for k, v in cats.items() if v)
        lines.append(f"- {r['scale']} / {label(r)}: {txt}")
    lines += ["", "### Coverage by stratum", ""]
    for r in runs:
        st = r["summary"]["coverage_by_stratum"]
        txt = "; ".join(
            f"{k} (n={v['subjects']}): {v.get('correct', '?')}/{v.get('total', '?')}"
            for k, v in st.items()
        )
        lines.append(f"- {r['scale']} / {label(r)}: {txt}")
    lines += ["", "## Charts", ""]
    lines += [f"![{c}](charts/{c})" for c in charts]
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", type=Path, default=RESULTS / "summary.json")
    ap.add_argument("--out", type=Path, default=RESULTS)
    args = ap.parse_args()
    for p in render(args.summary, args.out):
        print(p)  # CLI script output (not library code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
