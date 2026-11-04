import { describe, expect, it } from "vitest";
import type { BenchmarkRunOut, StepResultOut } from "../api/types";
import {
  formatCoverage,
  formatDateTime,
  formatDuration,
  formatInt,
  metricBars,
  numericMetricKeys,
  evidenceSummary,
  nodeLabel,
  shortHash,
  shortId,
  summarizeSteps,
} from "./format";

describe("basic formatting", () => {
  it("formats timestamps in UTC", () => {
    expect(formatDateTime("2026-11-04T10:20:30.123456Z")).toBe("2026-11-04 10:20:30 UTC");
    expect(formatDateTime("2026-11-04T12:20:30+02:00")).toBe("2026-11-04 10:20:30 UTC");
    expect(formatDateTime(null)).toBe("-");
    expect(formatDateTime("not a date")).toBe("not a date");
  });

  it("shortens hashes and ids but leaves short values alone", () => {
    expect(shortHash("a".repeat(64))).toBe("aaaaaaaaaaaa...");
    expect(shortHash("abc")).toBe("abc");
    expect(shortHash(null)).toBe("-");
    expect(shortId("12345678-aaaa")).toBe("12345678");
  });

  it("formats ints and durations", () => {
    expect(formatInt(1234567)).toBe("1,234,567");
    expect(formatInt(null)).toBe("-");
    expect(formatDuration(42)).toBe("42 ms");
    expect(formatDuration(1500)).toBe("1.50 s");
    expect(formatDuration(undefined)).toBe("-");
  });
});

describe("formatCoverage", () => {
  it("uses the exact integer counts, truncating the percentage", () => {
    expect(formatCoverage(2, 3)).toBe("2/3 (66.6%)");
    expect(formatCoverage(5, 5)).toBe("5/5 (100%)");
    expect(formatCoverage(0, 4)).toBe("0/4 (0%)");
  });
  it("treats required = 0 as full coverage (CR lead-1 §3)", () => {
    expect(formatCoverage(0, 0)).toBe("0/0 (100%)");
  });
});

describe("summarizeSteps", () => {
  const step = (status: StepResultOut["status"], category: StepResultOut["category"]): StepResultOut => ({
    step_order: 0,
    action: "DELETE_ROWS",
    category,
    table: "app.t",
    status,
    rows_affected: null,
    expected_rows: null,
    duration_ms: 1,
  });
  it("counts server statuses and splits skipped by category", () => {
    const s = summarizeSteps([
      step("APPLIED", "DELETE"),
      step("APPLIED", "RECOMPUTE"),
      step("FAILED", "DELETE"),
      step("SKIPPED", "UNRESOLVED"),
      step("SKIPPED", "SCHEDULE"),
      step("SKIPPED", "MANUAL_REVIEW"),
      step("SKIPPED", "RETAIN"),
    ]);
    expect(s).toEqual({
      total: 7,
      executed: 3,
      successful: 2,
      failed: 1,
      skipped: 4,
      skippedUnresolved: 1,
      skippedScheduled: 1,
      skippedManual: 1,
      skippedOther: 1,
    });
  });
  it("handles no steps", () => {
    expect(summarizeSteps([]).total).toBe(0);
  });
});

describe("benchmark helpers", () => {
  const run = (id: string, metrics: Record<string, unknown>): BenchmarkRunOut => ({
    id,
    scenario: "B1",
    params: {},
    seed: 42,
    git_commit: null,
    environment: {},
    metrics,
    created_at: "2026-11-04T00:00:00Z",
  });
  const runs = [
    run("11111111-x", { p50_ms: 12, p95_ms: 30, label: "x" }),
    run("22222222-x", { p50_ms: 15, nested: { a: 1 }, nan: Number.NaN }),
  ];
  it("finds sorted numeric top-level keys only", () => {
    expect(numericMetricKeys(runs)).toEqual(["p50_ms", "p95_ms"]);
  });
  it("omits runs without the metric instead of zero-filling", () => {
    expect(metricBars(runs, "p95_ms")).toEqual([{ id: "11111111-x", label: "B1 #11111111", value: 30 }]);
    expect(metricBars(runs, "p50_ms").map((b) => b.value)).toEqual([12, 15]);
  });
});

describe("evidenceSummary", () => {
  it("reads residual_scan suggestions with their hit_count", () => {
    expect(
      evidenceSummary({ evidence: "residual_scan", hit_count: 3, note: "value match" }),
    ).toMatchObject({ method: "residual_scan", hitCount: 3, note: "value match" });
  });
  it("reads sampled containment counts", () => {
    const s = evidenceSummary({ evidence: "name_pattern+sampled_containment", contained: 951, sampled: 953 });
    expect(s).toMatchObject({ contained: 951, sampled: 953, hitCount: null });
    expect(s.methodLabel).toMatch(/sampled/);
  });
  it("returns nulls for missing or non-numeric fields and keeps unknown methods verbatim", () => {
    expect(evidenceSummary(null)).toMatchObject({ method: null, hitCount: null, note: null });
    expect(evidenceSummary({ evidence: "x", hit_count: "3" })).toMatchObject({
      methodLabel: "x",
      hitCount: null,
    });
  });
});

describe("nodeLabel", () => {
  it("drops only the t:/c: kind prefix", () => {
    expect(nodeLabel("t:app.customer")).toBe("app.customer");
    expect(nodeLabel("c:app.customer.email")).toBe("app.customer.email");
    expect(nodeLabel("app.x")).toBe("app.x");
  });
});
