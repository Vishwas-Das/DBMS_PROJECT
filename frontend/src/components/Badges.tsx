import type { ReactNode } from "react";
import type {
  ActionCategory,
  EdgeStatus,
  ExecutionStatus,
  PlanStatus,
  RequestStatus,
  Severity,
  StepStatus,
  Traceability,
  VerificationOutcome,
  VerificationStatus,
} from "../api/types";
import { CATEGORY_LABEL, TRACEABILITY_LABEL } from "../lib/format";

/** Every badge shows text plus a symbol. Colour is never the only signal. */
type Tone = "green" | "amber" | "red" | "slate" | "blue" | "violet";

const TONE: Record<Tone, string> = {
  green: "bg-green-100 text-green-900 border-green-400",
  amber: "bg-amber-100 text-amber-900 border-amber-400",
  red: "bg-red-100 text-red-900 border-red-400",
  slate: "bg-slate-100 text-slate-800 border-slate-300",
  blue: "bg-blue-100 text-blue-900 border-blue-300",
  violet: "bg-violet-100 text-violet-900 border-violet-300",
};

export function Pill({
  tone,
  symbol,
  children,
  title,
}: {
  tone: Tone;
  symbol?: string;
  children: ReactNode;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded border px-1.5 py-0.5 text-xs font-medium ${TONE[tone]}`}
    >
      {symbol ? <span aria-hidden="true">{symbol}</span> : null}
      {children}
    </span>
  );
}

const OUTCOME: Record<VerificationOutcome, { tone: Tone; symbol: string }> = {
  VERIFIED: { tone: "green", symbol: "✓" },
  PARTIAL: { tone: "amber", symbol: "◐" },
  FAILED: { tone: "red", symbol: "✗" },
};

export function OutcomeBadge({ outcome }: { outcome: VerificationOutcome | null | undefined }) {
  if (!outcome) {
    return (
      <Pill tone="slate" symbol="–">
        NOT VERIFIED
      </Pill>
    );
  }
  const o = OUTCOME[outcome];
  return (
    <Pill tone={o.tone} symbol={o.symbol} title={`Verification outcome: ${outcome}`}>
      {outcome}
    </Pill>
  );
}

const STATUS_TONE: Record<string, Tone> = {
  // request / plan
  RECEIVED: "slate",
  PLANNED: "blue",
  DRAFT: "blue",
  BLOCKED: "red",
  APPROVED: "violet",
  SUPERSEDED: "slate",
  EXECUTED: "violet",
  VERIFIED: "green",
  FAILED: "red",
  CANCELLED: "slate",
  // execution / step / verification run
  RUNNING: "blue",
  COMMITTED: "green",
  ROLLED_BACK: "amber",
  APPLIED: "green",
  SKIPPED: "slate",
  PASSED: "green",
  ERROR: "red",
};

export function StatusPill({
  status,
}: {
  status: RequestStatus | PlanStatus | ExecutionStatus | StepStatus | VerificationStatus;
}) {
  return <Pill tone={STATUS_TONE[status] ?? "slate"}>{status}</Pill>;
}

export function PassFail({ passed }: { passed: boolean }) {
  return passed ? (
    <Pill tone="green" symbol="✓">
      PASS
    </Pill>
  ) : (
    <Pill tone="red" symbol="✗">
      FAIL
    </Pill>
  );
}

export function SeverityBadge({ severity }: { severity: Severity }) {
  return severity === "BLOCKING" ? (
    <Pill tone="red" symbol="⛔">
      BLOCKING
    </Pill>
  ) : (
    <Pill tone="amber" symbol="!">
      WARNING
    </Pill>
  );
}

const CATEGORY_TONE: Record<ActionCategory, Tone> = {
  DELETE: "red",
  INVALIDATE: "amber",
  RECOMPUTE: "blue",
  RETAIN: "green",
  SCHEDULE: "violet",
  MANUAL_REVIEW: "amber",
  UNRESOLVED: "slate",
};

export function CategoryBadge({ category }: { category: ActionCategory }) {
  return <Pill tone={CATEGORY_TONE[category]}>{CATEGORY_LABEL[category]}</Pill>;
}

const TRACE_SYMBOL: Record<Traceability, { tone: Tone; symbol: string }> = {
  KEY_PRESERVING: { tone: "green", symbol: "⚿" },
  AGGREGATE: { tone: "blue", symbol: "Σ" },
  OPAQUE: { tone: "slate", symbol: "?" },
};

export function TraceabilityBadge({ traceability }: { traceability: Traceability }) {
  const t = TRACE_SYMBOL[traceability];
  return (
    <Pill tone={t.tone} symbol={t.symbol}>
      {TRACEABILITY_LABEL[traceability]}
    </Pill>
  );
}

const EDGE_STATUS: Record<EdgeStatus, { tone: Tone; symbol: string }> = {
  CONFIRMED: { tone: "green", symbol: "✓" },
  SUGGESTED: { tone: "amber", symbol: "?" },
  REJECTED: { tone: "slate", symbol: "✗" },
};

export function EdgeStatusBadge({ status }: { status: EdgeStatus }) {
  const s = EDGE_STATUS[status];
  return (
    <Pill tone={s.tone} symbol={s.symbol}>
      {status}
    </Pill>
  );
}
