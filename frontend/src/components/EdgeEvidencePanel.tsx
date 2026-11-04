import type { ReactNode } from "react";
import type { GraphEdgeOut } from "../api/types";
import { ORIGIN_LABEL, evidenceSummary, formatInt, nodeLabel, prettyJson } from "../lib/format";
import { edgeVisual } from "../lib/graphAdapter";
import { EdgeStatusBadge, TraceabilityBadge } from "./Badges";

/** Shows one lineage edge exactly as the server returned it, including its `evidence`. */
export function EdgeEvidencePanel({
  edge,
  onClose,
  actions,
}: {
  edge: GraphEdgeOut;
  onClose?: () => void;
  /** Optional controls (e.g. confirm / reject for INFERRED edges). */
  actions?: ReactNode;
}) {
  const v = edgeVisual(edge.origin, edge.traceability, edge.status);
  const ev = evidenceSummary(edge.evidence);
  return (
    <section aria-label="Edge evidence" className="card min-w-0 space-y-2 text-sm">
      <div className="flex items-start justify-between gap-2">
        <h3 className="font-semibold break-all">
          Edge {edge.level.toLowerCase()}: {nodeLabel(edge.source)} &rarr; {nodeLabel(edge.target)}
        </h3>
        {onClose ? (
          <button type="button" className="btn-secondary" onClick={onClose}>
            Close
          </button>
        ) : null}
      </div>
      <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-1">
        <dt className="text-slate-500">Edge id</dt>
        <dd className="font-mono text-xs break-all">{edge.id}</dd>
        <dt className="text-slate-500">Origin</dt>
        <dd>
          {ORIGIN_LABEL[edge.origin]} <span className="text-slate-500">({v.pattern} line)</span>
        </dd>
        <dt className="text-slate-500">Traceability</dt>
        <dd>
          <TraceabilityBadge traceability={edge.traceability} />
        </dd>
        <dt className="text-slate-500">Status</dt>
        <dd>
          <EdgeStatusBadge status={edge.status} />
        </dd>
        <dt className="text-slate-500">Transform</dt>
        <dd>{edge.transform}</dd>
        <dt className="text-slate-500">Columns</dt>
        <dd className="font-mono text-xs">
          ({edge.source_columns.join(", ") || "-"}) &rarr; ({edge.target_columns.join(", ") || "-"})
        </dd>
        {edge.on_delete_action ? (
          <>
            <dt className="text-slate-500">ON DELETE</dt>
            <dd>{edge.on_delete_action}</dd>
          </>
        ) : null}
        {edge.confidence !== undefined ? (
          <>
            <dt className="text-slate-500">Confidence</dt>
            <dd>{edge.confidence}</dd>
          </>
        ) : null}
      </dl>
      <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-1 rounded border border-slate-200 p-2">
        <dt className="text-slate-500">Evidence method</dt>
        <dd>
          {ev.methodLabel}
          {ev.method ? <code className="ml-1 break-all text-xs">({ev.method})</code> : null}
        </dd>
        {ev.hitCount !== null ? (
          <>
            <dt className="text-slate-500">Residual hits</dt>
            <dd>{formatInt(ev.hitCount)} matching row(s) after erasure</dd>
          </>
        ) : null}
        {ev.contained !== null && ev.sampled !== null ? (
          <>
            <dt className="text-slate-500">Sampled containment</dt>
            <dd>
              {formatInt(ev.contained)} of {formatInt(ev.sampled)} sampled values found in the source
            </dd>
          </>
        ) : null}
        {ev.note ? (
          <>
            <dt className="text-slate-500">Note</dt>
            <dd className="break-words">{ev.note}</dd>
          </>
        ) : null}
      </dl>
      {actions}
      <div>
        <p className="font-medium">Evidence (raw, as returned by the server)</p>
        {edge.evidence && Object.keys(edge.evidence).length > 0 ? (
          <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-all rounded bg-slate-100 p-2 text-xs">
            {prettyJson(edge.evidence)}
          </pre>
        ) : (
          <p className="text-slate-500">The server returned no evidence for this edge.</p>
        )}
      </div>
    </section>
  );
}
