import type { GraphEdgeOut } from "../api/types";
import { ORIGIN_LABEL, prettyJson } from "../lib/format";
import { edgeVisual } from "../lib/graphAdapter";
import { EdgeStatusBadge, TraceabilityBadge } from "./Badges";

/** Shows one lineage edge exactly as the server returned it, including its `evidence`. */
export function EdgeEvidencePanel({
  edge,
  onClose,
}: {
  edge: GraphEdgeOut;
  onClose?: () => void;
}) {
  const v = edgeVisual(edge.origin, edge.traceability, edge.status);
  return (
    <section aria-label="Edge evidence" className="card space-y-2 text-sm">
      <div className="flex items-start justify-between gap-2">
        <h3 className="font-semibold">
          Edge {edge.level.toLowerCase()}: {edge.source} &rarr; {edge.target}
        </h3>
        {onClose ? (
          <button type="button" className="btn-secondary" onClick={onClose}>
            Close
          </button>
        ) : null}
      </div>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1">
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
      <div>
        <p className="font-medium">Evidence</p>
        {edge.evidence && Object.keys(edge.evidence).length > 0 ? (
          <pre className="max-h-72 overflow-auto rounded bg-slate-100 p-2 text-xs">
            {prettyJson(edge.evidence)}
          </pre>
        ) : (
          <p className="text-slate-500">The server returned no evidence for this edge.</p>
        )}
      </div>
    </section>
  );
}
