import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useCreateRequest, useCustomers, useRequests } from "../api/hooks";
import type { CustomerOut } from "../api/types";
import { OutcomeBadge, StatusPill } from "../components/Badges";
import { EmptyState, ErrorState, QueryState } from "../components/States";
import { formatDateTime, shortHash, shortId } from "../lib/format";

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

/**
 * The only place a subject key is handled: the selected customer's id is sent once in
 * POST /api/erasure-requests and is kept only in this component's state, never stored.
 */
function CreateRequestForm() {
  const [q, setQ] = useState("");
  const debounced = useDebounced(q.trim(), 300);
  const customers = useCustomers(debounced);
  const [selected, setSelected] = useState<CustomerOut | null>(null);
  const [reason, setReason] = useState("");
  const create = useCreateRequest();

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!selected) return;
    create.mutate(
      {
        subject_root: "customer",
        subject_key: [selected.id],
        reason: reason.trim() || null,
      },
      {
        onSuccess: () => {
          setSelected(null);
          setReason("");
        },
      },
    );
  };

  return (
    <section aria-labelledby="create-h" className="card space-y-3">
      <h2 id="create-h" className="section-title">
        New erasure request
      </h2>
      <div>
        <label htmlFor="customer-search" className="label">
          Search synthetic customers (name or masked email)
        </label>
        <input
          id="customer-search"
          type="search"
          className="input max-w-md"
          maxLength={64}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          autoComplete="off"
        />
      </div>

      <QueryState
        query={customers}
        errorTitle="Customer search failed"
        isEmpty={(c) => c.length === 0}
        empty={<EmptyState title="No matching customers" />}
      >
        {(rows) => (
          <table className="table" aria-label="Customer search results">
            <thead>
              <tr>
                <th scope="col">Select</th>
                <th scope="col">Name (synthetic)</th>
                <th scope="col">Email (masked)</th>
                <th scope="col">Open request</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => (
                <tr key={c.id} className={selected?.id === c.id ? "bg-blue-50" : undefined}>
                  <td>
                    <input
                      type="radio"
                      name="customer"
                      aria-label={`Select ${c.display_name}`}
                      checked={selected?.id === c.id}
                      onChange={() => setSelected(c)}
                    />
                  </td>
                  <td>{c.display_name}</td>
                  <td className="font-mono text-xs">{c.email_masked}</td>
                  <td>
                    {c.open_request_id ? (
                      <Link className="link font-mono" to={`/plan/${c.open_request_id}`}>
                        {shortId(c.open_request_id)}
                      </Link>
                    ) : (
                      "-"
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </QueryState>

      <form onSubmit={submit} className="space-y-2">
        <div>
          <label htmlFor="request-reason" className="label">
            Reason (optional, max 500 characters)
          </label>
          <textarea
            id="request-reason"
            className="input max-w-xl"
            maxLength={500}
            rows={2}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
        </div>
        <p className="text-sm">
          Selected subject:{" "}
          {selected ? (
            <strong>
              {selected.display_name} ({selected.email_masked})
            </strong>
          ) : (
            <span className="text-slate-500">none</span>
          )}
        </p>
        <button type="submit" className="btn-primary" disabled={!selected || create.isPending}>
          {create.isPending ? "Creating..." : "Create erasure request"}
        </button>
      </form>
      {create.error ? <ErrorState error={create.error} title="Could not create request" /> : null}
      {create.data ? (
        <p role="status" className="text-sm text-green-800">
          Created request <code>{create.data.id}</code> (status {create.data.status}).{" "}
          <Link className="link" to={`/plan/${create.data.id}`}>
            Continue to discovery and planning
          </Link>
        </p>
      ) : null}
    </section>
  );
}

export function RequestsPage() {
  const requests = useRequests();
  return (
    <div className="space-y-6">
      <h1 className="page-title">Erasure Requests</h1>
      <CreateRequestForm />
      <section aria-labelledby="list-h" className="space-y-2">
        <h2 id="list-h" className="section-title">
          All requests
        </h2>
        <QueryState
          query={requests}
          errorTitle="Could not load requests"
          isEmpty={(r) => r.length === 0}
          empty={<EmptyState title="No erasure requests yet" />}
        >
          {(rows) => (
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Request id</th>
                  <th scope="col">Root</th>
                  <th scope="col">Subject key HMAC</th>
                  <th scope="col">Status</th>
                  <th scope="col">Outcome</th>
                  <th scope="col">Requested by</th>
                  <th scope="col">Created</th>
                  <th scope="col">Completed</th>
                  <th scope="col">Go to</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id}>
                    <td className="font-mono text-xs">{r.id}</td>
                    <td>{r.subject_root}</td>
                    <td className="font-mono text-xs" title={r.subject_key_hmac}>
                      {shortHash(r.subject_key_hmac, 16)}
                    </td>
                    <td>
                      <StatusPill status={r.status} />
                    </td>
                    <td>
                      <OutcomeBadge outcome={r.latest_outcome} />
                    </td>
                    <td>{r.requested_by}</td>
                    <td>{formatDateTime(r.created_at)}</td>
                    <td>{formatDateTime(r.completed_at)}</td>
                    <td className="space-x-2 whitespace-nowrap">
                      <Link className="link" to={`/lineage/${r.id}`}>
                        Lineage
                      </Link>
                      <Link className="link" to={`/plan/${r.id}`}>
                        Plan
                      </Link>
                      <Link className="link" to={`/execution/${r.id}`}>
                        Execute
                      </Link>
                      <Link className="link" to={`/verification/${r.id}`}>
                        Verify
                      </Link>
                    </td>
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
