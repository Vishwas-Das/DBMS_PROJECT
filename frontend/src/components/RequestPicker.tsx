import { useId } from "react";
import { useNavigate } from "react-router-dom";
import { useRequests } from "../api/hooks";
import { formatDateTime, shortId } from "../lib/format";
import { ErrorState, Loading } from "./States";

/**
 * Picks an erasure request by id and navigates to `${basePath}/${id}`.
 * Shows only the request id, status and the subject key HMAC prefix. Never a raw key.
 */
export function RequestPicker({
  basePath,
  value,
  allowNone,
  noneLabel = "(none)",
}: {
  basePath: string;
  value: string | undefined;
  allowNone?: boolean;
  noneLabel?: string;
}) {
  const id = useId();
  const nav = useNavigate();
  const q = useRequests();
  if (q.error) return <ErrorState error={q.error} title="Could not load requests" />;
  if (q.isPending) return <Loading label="Loading requests" />;
  const rows = q.data;
  const known = value ? rows.some((r) => r.id === value) : true;
  return (
    <div className="flex flex-wrap items-end gap-2">
      <div>
        <label htmlFor={id} className="label">
          Erasure request
        </label>
        <select
          id={id}
          className="input min-w-[22rem]"
          value={value ?? ""}
          onChange={(e) => nav(e.target.value ? `${basePath}/${e.target.value}` : basePath)}
        >
          {allowNone || !value ? <option value="">{allowNone ? noneLabel : "Select..."}</option> : null}
          {!known && value ? <option value={value}>{value}</option> : null}
          {rows.map((r) => (
            <option key={r.id} value={r.id}>
              {shortId(r.id)} | {r.subject_root} | {r.status} | {formatDateTime(r.created_at)}
            </option>
          ))}
        </select>
      </div>
      {rows.length === 0 ? (
        <p className="text-sm text-slate-500">No erasure requests exist yet.</p>
      ) : null}
    </div>
  );
}
