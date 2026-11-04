import { useState, useSyncExternalStore } from "react";
import { actorFor } from "../api/client";
import { getCredentials, subscribeCredentials } from "../api/credentials";
import { requestOpenSettings } from "../components/SettingsDialog";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  useExecutePlan,
  useExecutions,
  useGeneratePlan,
  usePlan,
  useRequest,
} from "../api/hooks";
import type { ExecutionOut, PlanOut } from "../api/types";
import { CategoryBadge, OutcomeBadge, StatusPill } from "../components/Badges";
import { HashBadge } from "../components/HashBadge";
import { Modal } from "../components/Modal";
import { RequestPicker } from "../components/RequestPicker";
import { EmptyState, ErrorState, Loading, QueryState } from "../components/States";
import {
  formatCoverage,
  formatDateTime,
  formatDuration,
  formatInt,
  summarizeSteps,
} from "../lib/format";

function StepSummaryView({ ex }: { ex: ExecutionOut }) {
  const s = summarizeSteps(ex.steps);
  return (
    <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4" aria-label="Step summary">
      <div className="card">
        <dt className="text-xs uppercase text-slate-500">Executed</dt>
        <dd className="text-xl font-semibold">{s.executed}</dd>
      </div>
      <div className="card">
        <dt className="text-xs uppercase text-slate-500">Successful</dt>
        <dd className="text-xl font-semibold">{s.successful}</dd>
      </div>
      <div className="card">
        <dt className="text-xs uppercase text-slate-500">Failed</dt>
        <dd className="text-xl font-semibold">{s.failed}</dd>
      </div>
      <div className="card">
        <dt className="text-xs uppercase text-slate-500">Skipped</dt>
        <dd className="text-xl font-semibold">{s.skipped}</dd>
        <dd className="text-xs text-slate-600">
          unresolved {s.skippedUnresolved}, scheduled {s.skippedScheduled}, manual{" "}
          {s.skippedManual}
          {s.skippedOther ? `, other ${s.skippedOther}` : ""}
        </dd>
      </div>
    </dl>
  );
}

export function ExecutionResult({
  ex,
  currentPlanId,
  defaultOpen = true,
}: {
  ex: ExecutionOut;
  currentPlanId?: string | null;
  defaultOpen?: boolean;
}) {
  const timings = Object.entries(ex.timings_ms ?? {});
  const otherPlan = currentPlanId !== undefined && currentPlanId !== null && ex.plan_id !== currentPlanId;
  return (
    <details
      className="card space-y-3"
      aria-label={`Execution ${ex.id}`}
      open={defaultOpen}
    >
      <summary className="cursor-pointer font-semibold">
        {ex.mode === "LIVE" ? "LIVE execution" : "Dry run"} <StatusPill status={ex.status} />{" "}
        <span className="text-sm font-normal text-slate-600">
          started {formatDateTime(ex.started_at)}
          {otherPlan ? " (earlier plan version)" : ""}
        </span>
      </summary>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-slate-500">Execution id</dt>
        <dd className="font-mono text-xs break-all">{ex.id}</dd>
        <dt className="text-slate-500">Plan id</dt>
        <dd className="font-mono text-xs break-all">
          {ex.plan_id}
          {otherPlan ? " (not the current plan)" : ""}
        </dd>
        <dt className="text-slate-500">Started / finished</dt>
        <dd>
          {formatDateTime(ex.started_at)} / {formatDateTime(ex.finished_at)}
        </dd>
        {ex.evidence_id ? (
          <>
            <dt className="text-slate-500">Evidence id</dt>
            <dd className="font-mono text-xs break-all">{ex.evidence_id}</dd>
          </>
        ) : null}
        {timings.length > 0 ? (
          <>
            <dt className="text-slate-500">Timings</dt>
            <dd>{timings.map(([k, v]) => `${k}: ${formatDuration(v)}`).join(", ")}</dd>
          </>
        ) : null}
      </dl>
      {ex.error ? (
        <div role="alert" className="rounded border border-red-300 bg-red-50 p-2 text-sm">
          Execution error <code>{ex.error.code}</code>: {ex.error.message}
        </div>
      ) : null}
      {ex.mode === "DRY_RUN" ? (
        <p className="text-sm text-slate-600">
          A dry run executes the plan in a transaction that is always rolled back.
        </p>
      ) : null}
      <StepSummaryView ex={ex} />
      {ex.steps.length === 0 ? (
        <EmptyState title="The server returned no step results" />
      ) : (
        <div className="overflow-x-auto">
        <table className="table" aria-label="Step results">
          <thead>
            <tr>
              <th scope="col">#</th>
              <th scope="col">Table</th>
              <th scope="col">Category</th>
              <th scope="col">Action</th>
              <th scope="col">Status</th>
              <th scope="col">Rows affected</th>
              <th scope="col">Expected rows</th>
              <th scope="col">Duration</th>
            </tr>
          </thead>
          <tbody>
            {ex.steps.map((s) => (
              <tr key={s.step_order}>
                <td>{s.step_order}</td>
                <td className="font-mono text-xs">{s.table}</td>
                <td>
                  <CategoryBadge category={s.category} />
                </td>
                <td className="font-mono text-xs">{s.action}</td>
                <td>
                  <StatusPill status={s.status} />
                </td>
                <td>{formatInt(s.rows_affected)}</td>
                <td>{formatInt(s.expected_rows)}</td>
                <td>{formatDuration(s.duration_ms)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
      {ex.verification ? (
        <div className="text-sm">
          Verification: <OutcomeBadge outcome={ex.verification.outcome} /> coverage{" "}
          {formatCoverage(ex.verification.verified_artifacts, ex.verification.required_artifacts)}
          .{" "}
        </div>
      ) : null}
    </details>
  );
}

/** Which X-LAE-Actor this browser sends for execute calls, and the four-eyes rule. */
function ExecutorNote({ approvedBy }: { approvedBy: string | null }) {
  const creds = useSyncExternalStore(subscribeCredentials, getCredentials);
  const executor = actorFor(creds, "executor");
  return (
    <p className="text-sm" aria-label="Executor identity">
      Executing as <strong>{executor ?? "(no name set: server default actor)"}</strong>.{" "}
      <span className="text-slate-600">
        Four-eyes rule: the server rejects a LIVE execution by the plan&apos;s approver.
      </span>{" "}
      {approvedBy !== null && executor === approvedBy ? (
        <span className="text-amber-800">
          <span aria-hidden="true">! </span>This name equals the approver.{" "}
        </span>
      ) : null}
      <button type="button" className="link" onClick={requestOpenSettings}>
        Change executor name
      </button>
    </p>
  );
}

function ExecutePanel({ plan, requestId }: { plan: PlanOut; requestId: string }) {
  const exec = useExecutePlan(plan.id, requestId);
  const replan = useGeneratePlan(requestId);
  const nav = useNavigate();
  const runs = useExecutions(requestId);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [ack, setAck] = useState(false);

  const closeConfirm = () => {
    setConfirmOpen(false);
    setAck(false);
  };

  return (
    <div className="space-y-4">
      <section className="card space-y-2" aria-labelledby="exec-h">
        <h2 id="exec-h" className="section-title">
          Plan v{plan.version} <StatusPill status={plan.status} />
        </h2>
        <HashBadge label="plan_hash" value={plan.plan_hash} />
        <p className="text-sm">
          Approved:{" "}
          {plan.approved_at
            ? `${plan.approved_by ?? "unknown"} at ${formatDateTime(plan.approved_at)}`
            : "not approved (the server will refuse to execute)"}
        </p>
        <ExecutorNote approvedBy={plan.approved_by ?? null} />
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className="btn-secondary"
            disabled={exec.isPending}
            onClick={() => exec.mutate("DRY_RUN")}
          >
            Dry run
          </button>
          <button
            type="button"
            className="btn-danger"
            disabled={exec.isPending}
            onClick={() => setConfirmOpen(true)}
          >
            Execute LIVE...
          </button>
        </div>
        {exec.isPending ? <Loading label="Executing" /> : null}
        {exec.error ? (
          <ErrorState
            error={exec.error}
            title="Execution failed"
            onReplan={() =>
              replan.mutate(undefined, { onSuccess: () => nav(`/plan/${requestId}`) })
            }
            replanPending={replan.isPending}
          />
        ) : null}
        {replan.error ? <ErrorState error={replan.error} title="Re-plan failed" /> : null}
      </section>

      <Modal open={confirmOpen} title="Confirm LIVE execution" onClose={closeConfirm}>
        <div className="space-y-3 text-sm">
          <p>
            This permanently applies {plan.items.length} plan items for plan{" "}
            <code>{plan.id}</code> in one SERIALIZABLE transaction, then verifies and issues
            evidence. It cannot be undone.
          </p>
          <HashBadge label="plan_hash" value={plan.plan_hash} />
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />I
            understand that LIVE execution deletes or modifies data.
          </label>
          <div className="flex justify-end gap-2">
            <button type="button" className="btn-secondary" onClick={closeConfirm}>
              Cancel
            </button>
            <button
              type="button"
              className="btn-danger"
              disabled={!ack}
              onClick={() => {
                closeConfirm();
                exec.mutate("LIVE");
              }}
            >
              Execute LIVE now
            </button>
          </div>
        </div>
      </Modal>

      <section className="space-y-3" aria-labelledby="results-h">
        <h2 id="results-h" className="section-title">
          Execution runs for this request (newest first)
        </h2>
        <QueryState query={runs} errorTitle="Could not load past executions">
          {(list) =>
            list.length === 0 ? (
              <EmptyState title="No executions yet">
                Start a dry run above. Verification results are on the{" "}
                <Link className="link" to={`/verification/${requestId}`}>
                  Verification &amp; Audit
                </Link>{" "}
                page.
              </EmptyState>
            ) : (
              <>
                {list.map((r, i) => (
                  <ExecutionResult
                    key={r.id}
                    ex={r}
                    currentPlanId={plan.id}
                    defaultOpen={i === 0}
                  />
                ))}
              </>
            )
          }
        </QueryState>
      </section>
    </div>
  );
}

function ExecutionForRequest({ requestId }: { requestId: string }) {
  const request = useRequest(requestId);
  const plan = usePlan(request.data?.current_plan_id);
  return (
    <QueryState query={request} errorTitle="Could not load the request">
      {(r) =>
        !r.current_plan_id ? (
          <EmptyState title="This request has no plan yet">
            Generate and approve one on the{" "}
            <Link className="link" to={`/plan/${requestId}`}>
              Erasure Plan
            </Link>{" "}
            page.
          </EmptyState>
        ) : (
          <QueryState query={plan} errorTitle="Could not load the plan">
            {(p) => <ExecutePanel plan={p} requestId={requestId} />}
          </QueryState>
        )
      }
    </QueryState>
  );
}

export function ExecutionPage() {
  const { requestId } = useParams();
  return (
    <div className="space-y-4">
      <h1 className="page-title">Execution Monitor</h1>
      <RequestPicker basePath="/execution" value={requestId} />
      {requestId ? (
        <ExecutionForRequest requestId={requestId} />
      ) : (
        <EmptyState title="Select an erasure request" />
      )}
    </div>
  );
}
