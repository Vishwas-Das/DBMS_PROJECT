import { useMemo, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import {
  useAcknowledgeConflict,
  useApprovePlan,
  useGeneratePlan,
  useLineage,
  usePlan,
  useRequest,
  useRunDiscovery,
} from "../api/hooks";
import type {
  ActionCategory,
  DiscoveryOut,
  GraphEdgeOut,
  PlanConflictOut,
  PlanItemOut,
  PlanOut,
  RecordRole,
} from "../api/types";
import {
  CategoryBadge,
  SeverityBadge,
  StatusPill,
  TraceabilityBadge,
} from "../components/Badges";
import { EdgeEvidencePanel } from "../components/EdgeEvidencePanel";
import { HashBadge } from "../components/HashBadge";
import { RequestPicker } from "../components/RequestPicker";
import { EmptyState, ErrorState, Loading, QueryState } from "../components/States";
import {
  CATEGORY_LABEL,
  ORIGIN_LABEL,
  ROLE_LABEL,
  TRACEABILITY_LABEL,
  formatDateTime,
  formatDuration,
  formatInt,
  shortId,
} from "../lib/format";
import { indexEdges } from "../lib/graphAdapter";

// ------------------------------------------------------------------ discovery

function DiscoverySummary({ d }: { d: DiscoveryOut }) {
  return (
    <div className="card space-y-2 text-sm" aria-label="Discovery result">
      <p className="font-medium">
        Discovery finished in {formatDuration(d.duration_ms)} on snapshot{" "}
        <code>{shortId(d.snapshot_id)}</code>
      </p>
      <ul className="flex flex-wrap gap-4">
        <li>Tables: {formatInt(d.tables)}</li>
        <li>Table edges: {formatInt(d.table_edges)}</li>
        <li>Column edges: {formatInt(d.column_edges)}</li>
        <li>Records found: {formatInt(d.records_found)}</li>
      </ul>
      <p>
        Records by role:{" "}
        {(Object.entries(d.records_by_role) as [RecordRole, number][])
          .map(([r, n]) => `${ROLE_LABEL[r] ?? r}: ${n}`)
          .join(", ") || "none"}
      </p>
      <p>
        Affected artifacts:{" "}
        {Object.entries(d.affected_artifacts)
          .map(([a, t]) => `${a} (${TRACEABILITY_LABEL[t] ?? t})`)
          .join(", ") || "none"}
      </p>
      {d.external_dependencies.length > 0 ? (
        <p>External dependencies (not inspectable): {d.external_dependencies.join(", ")}</p>
      ) : null}
      <HashBadge label="record_set_hash" value={d.record_set_hash} />
      <HashBadge label="schema_fingerprint" value={d.schema_fingerprint} />
      {d.warnings.length > 0 ? (
        <div>
          <p className="font-medium text-amber-800">Warnings</p>
          <ul className="list-disc pl-5">
            {d.warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------------ plan tables

function CountsByCategory({ counts }: { counts: PlanOut["counts_by_category"] }) {
  const entries = Object.entries(counts) as [ActionCategory, number][];
  if (entries.length === 0) return <p className="text-sm text-slate-500">No items.</p>;
  return (
    <ul className="flex flex-wrap gap-3 text-sm" aria-label="Item counts per action category">
      {entries.map(([c, n]) => (
        <li key={c} className="flex items-center gap-1">
          <CategoryBadge category={c} /> <strong>{n}</strong>
        </li>
      ))}
    </ul>
  );
}

function EdgeRef({
  id,
  edge,
  onShow,
}: {
  id: string;
  edge: GraphEdgeOut | undefined;
  onShow: (e: GraphEdgeOut) => void;
}) {
  if (!edge) {
    return (
      <span className="font-mono text-xs text-slate-500" title="Edge not found in the lineage graph">
        {shortId(id)} (not in graph)
      </span>
    );
  }
  return (
    <button
      type="button"
      className="link text-left text-xs"
      onClick={() => onShow(edge)}
      aria-label={`Show evidence for edge ${edge.source} to ${edge.target}`}
    >
      {ORIGIN_LABEL[edge.origin]}: {edge.source} &rarr; {edge.target}
    </button>
  );
}

function ItemsTable({
  items,
  edges,
  onShowEdge,
}: {
  items: PlanItemOut[];
  edges: Map<string, GraphEdgeOut>;
  onShowEdge: (e: GraphEdgeOut) => void;
}) {
  if (items.length === 0) return <EmptyState title="The plan has no items" />;
  return (
    <div className="overflow-x-auto">
      <table className="table" aria-label="Plan items">
        <thead>
          <tr>
            <th scope="col">#</th>
            <th scope="col">Artifact</th>
            <th scope="col">Category</th>
            <th scope="col">Action</th>
            <th scope="col">Level</th>
            <th scope="col">Relationship (reason edges)</th>
            <th scope="col">Reason</th>
            <th scope="col">Retention policy</th>
            <th scope="col">Records</th>
            <th scope="col">Expected rows</th>
            <th scope="col">Depends on</th>
          </tr>
        </thead>
        <tbody>
          {[...items]
            .sort((a, b) => a.step_order - b.step_order)
            .map((it) => (
              <tr key={it.id}>
                <td>{it.step_order}</td>
                <td className="font-mono text-xs">
                  {it.schema_name}.{it.table_name}
                  {it.target_columns.length > 0 ? (
                    <div className="text-slate-500">cols: {it.target_columns.join(", ")}</div>
                  ) : null}
                </td>
                <td>
                  <CategoryBadge category={it.category} />
                </td>
                <td className="font-mono text-xs">{it.action}</td>
                <td>{it.lineage_level}</td>
                <td>
                  {it.reason_edge_ids.length === 0 ? (
                    <span className="text-slate-500">root / none</span>
                  ) : (
                    <ul className="space-y-0.5">
                      {it.reason_edge_ids.map((eid) => {
                        const e = edges.get(eid);
                        return (
                          <li key={eid} className="flex flex-wrap items-center gap-1">
                            <EdgeRef id={eid} edge={e} onShow={onShowEdge} />
                            {e ? <TraceabilityBadge traceability={e.traceability} /> : null}
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </td>
                <td className="max-w-xs">{it.reason}</td>
                <td>{it.retention_policy ?? "-"}</td>
                <td>{formatInt(it.record_count)}</td>
                <td>{formatInt(it.expected_row_count)}</td>
                <td>{it.depends_on_steps.length ? it.depends_on_steps.join(", ") : "-"}</td>
              </tr>
            ))}
        </tbody>
      </table>
    </div>
  );
}

function ConflictsTable({ plan, requestId }: { plan: PlanOut; requestId: string }) {
  const ack = useAcknowledgeConflict(plan.id, requestId);
  if (plan.conflicts.length === 0) {
    return <EmptyState title="No conflicts reported by the planner" />;
  }
  return (
    <div className="space-y-2">
      <table className="table" aria-label="Plan conflicts">
        <thead>
          <tr>
            <th scope="col">Severity</th>
            <th scope="col">Code</th>
            <th scope="col">Artifact</th>
            <th scope="col">Message</th>
            <th scope="col">Resolution</th>
            <th scope="col">Action</th>
          </tr>
        </thead>
        <tbody>
          {plan.conflicts.map((c: PlanConflictOut) => (
            <tr key={c.id}>
              <td>
                <SeverityBadge severity={c.severity} />
              </td>
              <td className="font-mono text-xs">{c.code}</td>
              <td className="font-mono text-xs">
                {c.schema_name}.{c.table_name}
              </td>
              <td className="max-w-md">{c.message}</td>
              <td>{c.resolution ?? <span className="text-slate-500">unresolved</span>}</td>
              <td>
                {c.acknowledgeable && !c.resolution ? (
                  <button
                    type="button"
                    className="btn-secondary"
                    disabled={ack.isPending}
                    onClick={() => ack.mutate({ conflictId: c.id })}
                  >
                    Acknowledge
                  </button>
                ) : c.acknowledgeable ? (
                  "acknowledged"
                ) : (
                  <span className="text-slate-500">not acknowledgeable</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {ack.error ? <ErrorState error={ack.error} title="Acknowledge failed" /> : null}
    </div>
  );
}

// ------------------------------------------------------------------ approval

/**
 * Shows the full plan hash and the per-category counts. Approve sends `plan.plan_hash`
 * exactly as received from the server. The UI never computes or edits it.
 */
export function ApprovePanel({
  plan,
  requestId,
  onReplan,
  replanPending,
}: {
  plan: PlanOut;
  requestId: string;
  onReplan?: () => void;
  replanPending?: boolean;
}) {
  const approve = useApprovePlan(plan.id, requestId);
  const already = plan.approved_at !== null && plan.approved_at !== undefined;
  return (
    <section aria-labelledby="approve-h" className="card space-y-2">
      <h2 id="approve-h" className="section-title">
        Approval
      </h2>
      <HashBadge label="plan_hash" value={plan.plan_hash} />
      <CountsByCategory counts={plan.counts_by_category} />
      {already ? (
        <p className="text-sm">
          Approved by <strong>{plan.approved_by ?? "unknown"}</strong> at{" "}
          {formatDateTime(plan.approved_at)}.
        </p>
      ) : null}
      {plan.is_blocked ? (
        <p className="text-sm text-red-800" role="note">
          <span aria-hidden="true">&#9940; </span>The server reports this plan as blocked.
          Acknowledge the acknowledgeable conflicts or re-plan.
        </p>
      ) : null}
      <button
        type="button"
        className="btn-primary"
        disabled={plan.is_blocked || already || approve.isPending}
        onClick={() => approve.mutate(plan.plan_hash)}
      >
        {approve.isPending ? "Approving..." : "Approve this plan hash"}
      </button>
      {approve.error ? (
        <ErrorState
          error={approve.error}
          title="Approval failed"
          onReplan={onReplan}
          replanPending={replanPending}
        />
      ) : null}
      {approve.data ? (
        <p role="status" className="text-sm text-green-800">
          Plan approved (status {approve.data.status}).{" "}
          <Link className="link" to={`/execution/${requestId}`}>
            Go to the Execution Monitor
          </Link>
        </p>
      ) : null}
    </section>
  );
}

// ------------------------------------------------------------------ page

function GenerateForm({ requestId }: { requestId: string }) {
  const gen = useGeneratePlan(requestId);
  const [maxDepth, setMaxDepth] = useState("10");
  const [maxRecords, setMaxRecords] = useState("100000");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    gen.mutate({ max_depth: Number(maxDepth), max_records: Number(maxRecords) });
  };
  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-3">
      <div>
        <label htmlFor="max-depth" className="label">
          Max depth (1-50)
        </label>
        <input
          id="max-depth"
          type="number"
          min={1}
          max={50}
          required
          className="input w-28"
          value={maxDepth}
          onChange={(e) => setMaxDepth(e.target.value)}
        />
      </div>
      <div>
        <label htmlFor="max-records" className="label">
          Max records
        </label>
        <input
          id="max-records"
          type="number"
          min={1}
          max={10000000}
          required
          className="input w-36"
          value={maxRecords}
          onChange={(e) => setMaxRecords(e.target.value)}
        />
      </div>
      <button type="submit" className="btn-primary" disabled={gen.isPending}>
        {gen.isPending ? "Generating..." : "Generate plan"}
      </button>
      {gen.error ? (
        <div className="w-full">
          <ErrorState error={gen.error} title="Plan generation failed" />
        </div>
      ) : null}
    </form>
  );
}

function PlanDetails({ plan, requestId }: { plan: PlanOut; requestId: string }) {
  const table = useLineage(requestId, "TABLE");
  const column = useLineage(requestId, "COLUMN");
  const edges = useMemo(
    () => indexEdges(table.data?.schema_graph, column.data?.schema_graph),
    [table.data, column.data],
  );
  const [shown, setShown] = useState<GraphEdgeOut | null>(null);
  const replan = useGeneratePlan(requestId);

  return (
    <div className="space-y-4">
      <section aria-labelledby="plan-h" className="card space-y-2 text-sm">
        <h2 id="plan-h" className="section-title">
          Plan v{plan.version} <StatusPill status={plan.status} />
          {plan.is_blocked ? <span className="ml-2 text-red-800">(blocked)</span> : null}
        </h2>
        <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1">
          <dt className="text-slate-500">Plan id</dt>
          <dd className="font-mono text-xs">{plan.id}</dd>
          <dt className="text-slate-500">Created</dt>
          <dd>{formatDateTime(plan.created_at)}</dd>
          <dt className="text-slate-500">Snapshot</dt>
          <dd className="font-mono text-xs">{plan.snapshot_id}</dd>
          <dt className="text-slate-500">Records</dt>
          <dd>{formatInt(plan.record_count)}</dd>
        </dl>
        <HashBadge label="schema_fingerprint" value={plan.schema_fingerprint} />
        <HashBadge label="record_set_hash" value={plan.record_set_hash} />
      </section>

      <section aria-labelledby="items-h" className="space-y-2">
        <h2 id="items-h" className="section-title">
          Items ({plan.items.length})
        </h2>
        <p className="text-xs text-slate-500">
          Categories: {Object.values(CATEGORY_LABEL).join(", ")}. The fine-grained action is the
          executable contract.
        </p>
        {table.error || column.error ? (
          <ErrorState
            error={table.error ?? column.error}
            title="Lineage edges could not be loaded, so reason edges are shown by id"
          />
        ) : null}
        <ItemsTable items={plan.items} edges={edges} onShowEdge={setShown} />
        {shown ? <EdgeEvidencePanel edge={shown} onClose={() => setShown(null)} /> : null}
      </section>

      <section aria-labelledby="conflicts-h" className="space-y-2">
        <h2 id="conflicts-h" className="section-title">
          Conflicts ({plan.conflicts.length})
        </h2>
        <ConflictsTable plan={plan} requestId={requestId} />
      </section>

      <ApprovePanel
        plan={plan}
        requestId={requestId}
        onReplan={() => replan.mutate(undefined)}
        replanPending={replan.isPending}
      />
      {replan.error ? <ErrorState error={replan.error} title="Re-plan failed" /> : null}
    </div>
  );
}

export function PlanPage() {
  const { requestId } = useParams();
  return (
    <div className="space-y-4">
      <h1 className="page-title">Erasure Plan</h1>
      <RequestPicker basePath="/plan" value={requestId} />
      {requestId ? (
        <PlanForRequest requestId={requestId} />
      ) : (
        <EmptyState title="Select an erasure request" />
      )}
    </div>
  );
}

function PlanForRequest({ requestId }: { requestId: string }) {
  const request = useRequest(requestId);
  const discovery = useRunDiscovery(requestId);
  const planId = request.data?.current_plan_id ?? null;
  const plan = usePlan(planId);

  return (
    <div className="space-y-4">
      <QueryState query={request} errorTitle="Could not load the request">
        {(r) => (
          <section className="card space-y-3" aria-labelledby="workflow-h">
            <h2 id="workflow-h" className="section-title">
              Request <code className="text-sm">{r.id}</code> <StatusPill status={r.status} />
            </h2>
            <div className="flex flex-wrap items-center gap-3">
              <button
                type="button"
                className="btn-secondary"
                disabled={discovery.isPending}
                onClick={() => discovery.mutate()}
              >
                {discovery.isPending ? "Discovering..." : "1. Run discovery"}
              </button>
              <span className="text-sm text-slate-500">
                Executes discovery queries for this subject (record-level lineage).
              </span>
            </div>
            {discovery.error ? (
              <ErrorState error={discovery.error} title="Discovery failed" />
            ) : null}
            {discovery.data ? <DiscoverySummary d={discovery.data} /> : null}
            <p className="text-sm font-medium">2. Generate a plan</p>
            <GenerateForm requestId={requestId} />
          </section>
        )}
      </QueryState>

      {request.data && !planId ? (
        <EmptyState title="No plan for this request yet">
          Run discovery and generate a plan.
        </EmptyState>
      ) : null}
      {planId ? (
        plan.error ? (
          <ErrorState error={plan.error} title="Could not load the plan" onRetry={() => void plan.refetch()} />
        ) : plan.data ? (
          <PlanDetails plan={plan.data} requestId={requestId} />
        ) : (
          <Loading label="Loading plan" />
        )
      ) : null}
    </div>
  );
}
