import { Link } from "react-router-dom";
import { useHealth, useRequests, useStats } from "../api/hooks";
import type { StatsOut } from "../api/types";
import { OutcomeBadge, Pill, StatusPill } from "../components/Badges";
import { EmptyState, QueryState } from "../components/States";
import { formatDateTime, formatInt, shortHash, shortId } from "../lib/format";

const STAT_FIELDS: ReadonlyArray<{ key: keyof StatsOut; label: string }> = [
  { key: "synthetic_customers", label: "Synthetic customers" },
  { key: "erasure_requests", label: "Erasure requests" },
  { key: "verified_requests", label: "Verified" },
  { key: "partial_requests", label: "Partial" },
  { key: "pending_requests", label: "Pending" },
  { key: "failed_requests", label: "Failed" },
  { key: "discovered_tables", label: "Discovered tables" },
  { key: "discovered_edges", label: "Discovered edges" },
  { key: "registered_artifacts", label: "Registered artifacts" },
  { key: "audit_events", label: "Audit events" },
];

export function DashboardPage() {
  const stats = useStats();
  const health = useHealth();
  const requests = useRequests();

  return (
    <div className="space-y-6">
      <h1 className="page-title">Dashboard</h1>

      <section aria-labelledby="stats-h" className="space-y-2">
        <h2 id="stats-h" className="section-title">
          Live counts
        </h2>
        <QueryState query={stats} errorTitle="Could not load statistics">
          {(s) => (
            <>
              <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
                {STAT_FIELDS.map((f) => (
                  <div key={f.key} className="card">
                    <dt className="text-xs uppercase text-slate-500">{f.label}</dt>
                    <dd className="text-2xl font-semibold" data-testid={`stat-${f.key}`}>
                      {formatInt(s[f.key] as number)}
                    </dd>
                  </div>
                ))}
              </dl>
              <p className="text-xs text-slate-500">
                Latest snapshot:{" "}
                {s.latest_snapshot_id ? (
                  <>
                    <code>{s.latest_snapshot_id}</code> at {formatDateTime(s.latest_snapshot_at)}
                  </>
                ) : (
                  "none yet"
                )}
              </p>
            </>
          )}
        </QueryState>
      </section>

      <section aria-labelledby="health-h" className="space-y-2">
        <h2 id="health-h" className="section-title">
          Health
        </h2>
        <QueryState query={health} errorTitle="Health check failed">
          {(h) => (
            <dl className="card grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
              <dt className="text-slate-500">Status</dt>
              <dd>
                <Pill tone={h.status === "ok" ? "green" : "amber"}>{h.status}</Pill>
              </dd>
              <dt className="text-slate-500">Database</dt>
              <dd>{h.database ? "reachable" : "unreachable"}</dd>
              <dt className="text-slate-500">pgvector</dt>
              <dd>{h.pgvector ?? "not installed"}</dd>
              <dt className="text-slate-500">pgcrypto</dt>
              <dd>{h.pgcrypto ?? "not installed"}</dd>
              <dt className="text-slate-500">Audit chain</dt>
              <dd>
                {h.audit_chain_valid === null
                  ? "not checked"
                  : h.audit_chain_valid
                    ? "valid"
                    : "INVALID"}
              </dd>
              <dt className="text-slate-500">Signing key</dt>
              <dd className="font-mono text-xs">{h.signing_key_id ?? "none configured"}</dd>
            </dl>
          )}
        </QueryState>
      </section>

      <section aria-labelledby="recent-h" className="space-y-2">
        <h2 id="recent-h" className="section-title">
          Recent erasure requests
        </h2>
        <QueryState
          query={requests}
          errorTitle="Could not load requests"
          isEmpty={(r) => r.length === 0}
          empty={
            <EmptyState title="No erasure requests yet">
              Create one on the <Link to="/requests" className="link">Erasure Requests</Link> page.
            </EmptyState>
          }
        >
          {(rows) => (
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Request</th>
                  <th scope="col">Root</th>
                  <th scope="col">Subject key HMAC</th>
                  <th scope="col">Status</th>
                  <th scope="col">Outcome</th>
                  <th scope="col">Created</th>
                </tr>
              </thead>
              <tbody>
                {rows.slice(0, 10).map((r) => (
                  <tr key={r.id}>
                    <td>
                      <Link className="link font-mono" to={`/plan/${r.id}`}>
                        {shortId(r.id)}
                      </Link>
                    </td>
                    <td>{r.subject_root}</td>
                    <td className="font-mono text-xs">{shortHash(r.subject_key_hmac, 16)}</td>
                    <td>
                      <StatusPill status={r.status} />
                    </td>
                    <td>
                      <OutcomeBadge outcome={r.latest_outcome} />
                    </td>
                    <td>{formatDateTime(r.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </QueryState>
      </section>
    </div>
  );
}
