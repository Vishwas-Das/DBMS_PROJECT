import { useSetEdgeStatus } from "../api/hooks";
import type { EdgeStatusUpdate, GraphEdgeOut } from "../api/types";
import { EdgeStatusBadge } from "./Badges";
import { ErrorState } from "./States";

type Decision = EdgeStatusUpdate["status"];

/** Explains what a human decision on an INFERRED edge does. Shown next to the review controls. */
export function EdgeReviewHelp() {
  return (
    <p className="text-xs text-slate-600">
      Inferred edges stay SUGGESTED and are never traversed until a person confirms them. A
      CONFIRMED edge is used by the <strong>next</strong> discovery and plan generation (for every
      request on this snapshot). Existing plans are not changed: re-run discovery and re-generate
      the plan to include it. A REJECTED edge is ignored and hidden from the graph.
    </p>
  );
}

/**
 * Confirm / reject controls for one INFERRED edge (POST /api/lineage-edges/{id}/status).
 * Only the server decides whether the change is allowed; the UI shows its error code otherwise.
 */
export function EdgeReview({ edge }: { edge: GraphEdgeOut }) {
  const m = useSetEdgeStatus();
  if (edge.origin !== "INFERRED") {
    return (
      <p className="text-xs text-slate-500">
        Only INFERRED edges can be confirmed or rejected; this edge comes from the catalog or a
        registration.
      </p>
    );
  }
  const decide = (status: Decision) => m.mutate({ edgeId: edge.id, body: { status } });
  const pending = m.isPending;
  return (
    <div className="space-y-2 rounded border border-amber-300 bg-amber-50 p-2" aria-label="Edge review">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm">
          Current status: <EdgeStatusBadge status={edge.status} />
        </span>
        <button
          type="button"
          className="btn-primary"
          disabled={pending || edge.status === "CONFIRMED"}
          onClick={() => decide("CONFIRMED")}
          aria-label={`Confirm edge ${edge.source} to ${edge.target}`}
        >
          Confirm
        </button>
        <button
          type="button"
          className="btn-secondary"
          disabled={pending || edge.status === "REJECTED"}
          onClick={() => decide("REJECTED")}
          aria-label={`Reject edge ${edge.source} to ${edge.target}`}
        >
          Reject
        </button>
        {pending ? <span className="text-sm">Saving...</span> : null}
      </div>
      {m.data ? (
        <p role="status" className="text-sm text-green-800">
          Saved: the server now reports this edge as {m.data.status}.
          {m.data.status === "CONFIRMED"
            ? " Re-run discovery and re-generate the plan to use it."
            : ""}
        </p>
      ) : null}
      {m.error ? <ErrorState error={m.error} title="Edge status change failed" /> : null}
      <EdgeReviewHelp />
    </div>
  );
}
