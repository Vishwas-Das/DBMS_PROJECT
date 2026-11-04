import type { ReactNode } from "react";
import { ApiError, CLIENT_ERROR_CODES } from "../api/client";
import { prettyJson } from "../lib/format";

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <p role="status" aria-live="polite" className="text-sm text-slate-500">
      {label}...
    </p>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div
      role="status"
      className="rounded border border-dashed border-slate-300 bg-white p-4 text-sm text-slate-600"
    >
      <p className="font-medium text-slate-700">{title}</p>
      {children ? <div className="mt-1">{children}</div> : null}
    </div>
  );
}

function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error;
  return new ApiError(0, "CLIENT_ERROR", error instanceof Error ? error.message : String(error));
}

const HINTS: Record<string, string> = {
  [CLIENT_ERROR_CODES.network]:
    "The backend is not reachable. Start the API on port 8000 (the dev server proxies /api).",
  HTTP_502:
    "The dev/preview proxy could not reach the backend (http://localhost:8000). Is the API running?",
  HTTP_504: "The backend did not answer in time.",
  UNAUTHORIZED:"Set a valid API token in Settings (top right). Mutations need X-LAE-Token.",
  STALE_PLAN: "The schema or the subject's records changed since this plan was generated.",
  PLAN_HASH_MISMATCH: "The plan changed after you loaded it. Reload it and review again.",
  PLAN_BLOCKED: "Resolve or acknowledge the blocking conflicts first.",
  SERIALIZATION_CONFLICT: "A concurrent transaction conflicted. Retry the action.",
};

export interface ErrorStateProps {
  error: unknown;
  title?: string;
  onRetry?: () => void;
  /** Shown when the error is STALE_PLAN (module_contracts §10: offer a re-plan). */
  onReplan?: () => void;
  replanPending?: boolean;
}

export function ErrorState({ error, title, onRetry, onReplan, replanPending }: ErrorStateProps) {
  const e = toApiError(error);
  const hasDetails = Object.keys(e.details).length > 0;
  return (
    <div role="alert" className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-900">
      <p className="font-semibold">
        {title ?? "Request failed"}: <code className="rounded bg-red-100 px-1">{e.code}</code>
        {e.status > 0 ? <span className="ml-2 font-normal">(HTTP {e.status})</span> : null}
      </p>
      {e.message ? <p className="mt-1">{e.message}</p> : null}
      {HINTS[e.code] ? <p className="mt-1 text-red-800">{HINTS[e.code]}</p> : null}
      {hasDetails ? (
        <details className="mt-2">
          <summary className="cursor-pointer">Details</summary>
          <pre className="mt-1 max-h-48 overflow-auto rounded bg-white p-2 text-xs">
            {prettyJson(e.details)}
          </pre>
        </details>
      ) : null}
      <div className="mt-2 flex gap-2">
        {onRetry ? (
          <button type="button" className="btn-secondary" onClick={onRetry}>
            Retry
          </button>
        ) : null}
        {e.code === "STALE_PLAN" && onReplan ? (
          <button
            type="button"
            className="btn-primary"
            onClick={onReplan}
            disabled={replanPending}
          >
            {replanPending ? "Re-planning..." : "Re-plan"}
          </button>
        ) : null}
      </div>
    </div>
  );
}

/** Standard rendering of a query: loading, error (with code), empty, or children. */
export function QueryState<T>({
  query,
  isEmpty,
  empty,
  children,
  errorTitle,
}: {
  query: { data: T | undefined; error: unknown; isPending: boolean; refetch: () => unknown };
  isEmpty?: (d: T) => boolean;
  empty?: ReactNode;
  errorTitle?: string;
  children: (data: T) => ReactNode;
}) {
  if (query.error) {
    return (
      <ErrorState error={query.error} title={errorTitle} onRetry={() => void query.refetch()} />
    );
  }
  if (query.isPending || query.data === undefined) return <Loading />;
  if (isEmpty?.(query.data)) return <>{empty ?? <EmptyState title="Nothing to show" />}</>;
  return <>{children(query.data)}</>;
}
