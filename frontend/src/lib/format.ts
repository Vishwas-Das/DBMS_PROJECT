/**
 * Pure display helpers. They format server values. They never decide anything
 * (no plan, verdict or hash computation).
 */
import type {
  ActionCategory,
  BenchmarkRunOut,
  EdgeOrigin,
  RecordRole,
  StepResultOut,
  Traceability,
} from "../api/types";

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  // Always UTC, ISO-like, to match the audit trail.
  return `${d.toISOString().slice(0, 19).replace("T", " ")} UTC`;
}

export function shortHash(hash: string | null | undefined, n = 12): string {
  if (!hash) return "-";
  return hash.length <= n ? hash : `${hash.slice(0, n)}...`;
}

export function shortId(id: string | null | undefined): string {
  if (!id) return "-";
  return id.length > 8 ? id.slice(0, 8) : id;
}

export function formatInt(n: number | null | undefined): string {
  if (n === null || n === undefined) return "-";
  return new Intl.NumberFormat("en-US").format(n);
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "-";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

/**
 * Coverage text from the exact integer counts the server returns. 1.0 when required = 0
 * (CR lead-1 §3). The percentage is for display only.
 */
export function formatCoverage(verified: number, required: number): string {
  if (required === 0) return "0/0 (100%)";
  const pct = Math.floor((verified * 1000) / required) / 10;
  return `${verified}/${required} (${pct}%)`;
}

export const CATEGORY_LABEL: Record<ActionCategory, string> = {
  DELETE: "Delete",
  INVALIDATE: "Invalidate",
  RECOMPUTE: "Recompute",
  RETAIN: "Retain",
  SCHEDULE: "Schedule",
  MANUAL_REVIEW: "Manual review",
  UNRESOLVED: "Unresolved",
};

export const ROLE_LABEL: Record<RecordRole, string> = {
  SUBJECT_ROOT: "Subject root",
  OWNED: "Owned",
  REFERENCING_OTHER_SUBJECT: "Other subject",
  SHARED: "Shared",
};

export const ORIGIN_LABEL: Record<EdgeOrigin, string> = {
  DECLARED_FK: "FK",
  SQL_LINEAGE: "SQL",
  REGISTERED_MAPPING: "Registered",
  INFERRED: "Inferred",
};

export const TRACEABILITY_LABEL: Record<Traceability, string> = {
  KEY_PRESERVING: "key-preserving",
  AGGREGATE: "aggregate",
  OPAQUE: "opaque",
};

export const TRACEABILITY_SHORT: Record<Traceability, string> = {
  KEY_PRESERVING: "KEY",
  AGGREGATE: "AGG",
  OPAQUE: "OPAQUE",
};

export interface StepSummary {
  total: number;
  executed: number;
  successful: number;
  failed: number;
  skipped: number;
  skippedUnresolved: number;
  skippedScheduled: number;
  skippedManual: number;
  skippedOther: number;
}

/** Counts the server's step statuses for display. No status is inferred. */
export function summarizeSteps(steps: readonly StepResultOut[]): StepSummary {
  const s: StepSummary = {
    total: steps.length,
    executed: 0,
    successful: 0,
    failed: 0,
    skipped: 0,
    skippedUnresolved: 0,
    skippedScheduled: 0,
    skippedManual: 0,
    skippedOther: 0,
  };
  for (const st of steps) {
    if (st.status === "APPLIED") {
      s.executed += 1;
      s.successful += 1;
    } else if (st.status === "FAILED") {
      s.executed += 1;
      s.failed += 1;
    } else {
      s.skipped += 1;
      if (st.category === "UNRESOLVED") s.skippedUnresolved += 1;
      else if (st.category === "SCHEDULE") s.skippedScheduled += 1;
      else if (st.category === "MANUAL_REVIEW") s.skippedManual += 1;
      else s.skippedOther += 1;
    }
  }
  return s;
}

/** Pretty JSON for evidence and payload panels (server data, shown verbatim). */
export function prettyJson(v: unknown): string {
  try {
    return JSON.stringify(v, null, 2) ?? "null";
  } catch {
    return String(v);
  }
}

// ------------------------------------------------------------------ benchmarks

/** Top-level metric keys whose value is a finite number in at least one run, sorted. */
export function numericMetricKeys(runs: readonly BenchmarkRunOut[]): string[] {
  const keys = new Set<string>();
  for (const r of runs) {
    for (const [k, v] of Object.entries(r.metrics)) {
      if (typeof v === "number" && Number.isFinite(v)) keys.add(k);
    }
  }
  return [...keys].sort();
}

export interface Bar {
  id: string;
  label: string;
  value: number;
}

/** Bars for one metric. Runs without a numeric value for it are omitted, not zero-filled. */
export function metricBars(runs: readonly BenchmarkRunOut[], key: string): Bar[] {
  const bars: Bar[] = [];
  for (const r of runs) {
    const v = r.metrics[key];
    if (typeof v === "number" && Number.isFinite(v)) {
      bars.push({ id: r.id, label: `${r.scenario} #${shortId(r.id)}`, value: v });
    }
  }
  return bars;
}

/** Residual hit rows: shows the known keys if present, otherwise null fields. */
export interface ResidualRow {
  table: string | null;
  column: string | null;
  count: number | null;
  raw: Record<string, unknown>;
}

function str(v: unknown): string | null {
  return typeof v === "string" ? v : null;
}

export function residualRow(hit: Record<string, unknown>): ResidualRow {
  const schema = str(hit.schema_name) ?? str(hit.schema);
  const table = str(hit.table_name) ?? str(hit.table);
  const fq = table && schema && !table.includes(".") ? `${schema}.${table}` : table;
  const countRaw = hit.count ?? hit.hits ?? hit.row_count;
  return {
    table: fq,
    column: str(hit.column_name) ?? str(hit.column),
    count: typeof countRaw === "number" ? countRaw : null,
    raw: hit,
  };
}
