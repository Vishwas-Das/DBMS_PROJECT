import { useParams } from "react-router-dom";
import { ApiError } from "../api/client";
import {
  useAuditEvents,
  useAuditForRequest,
  useDownloadEvidence,
  useDownloadEvidenceLeaves,
  useRunVerification,
  useVerification,
  useVerifyChain,
} from "../api/hooks";
import type {
  AuditEventOut,
  ChainVerificationOut,
  EvidenceOut,
  VerificationOut,
} from "../api/types";
import { OutcomeBadge, PassFail, Pill, StatusPill } from "../components/Badges";
import { HashBadge } from "../components/HashBadge";
import { RequestPicker } from "../components/RequestPicker";
import { EmptyState, ErrorState, Loading } from "../components/States";
import {
  formatCoverage,
  formatDateTime,
  formatDuration,
  formatInt,
  prettyJson,
  residualRow,
  shortHash,
} from "../lib/format";

function isNotFound(e: unknown): boolean {
  return e instanceof ApiError && e.status === 404;
}

function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

// ------------------------------------------------------------------ verification

export function VerificationSummary({ v }: { v: VerificationOut }) {
  return (
    <section className="card space-y-3" aria-labelledby="ver-h">
      <h2 id="ver-h" className="section-title flex flex-wrap items-center gap-2">
        Verification outcome <OutcomeBadge outcome={v.outcome} />
        <span className="text-sm font-normal">
          run status <StatusPill status={v.status} />
        </span>
      </h2>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-slate-500">Coverage (verified / required)</dt>
        <dd data-testid="coverage">{formatCoverage(v.verified_artifacts, v.required_artifacts)}</dd>
        <dt className="text-slate-500">Verification id</dt>
        <dd className="font-mono text-xs">{v.id}</dd>
        <dt className="text-slate-500">Execution run</dt>
        <dd className="font-mono text-xs">{v.execution_run_id}</dd>
        <dt className="text-slate-500">Started / finished</dt>
        <dd>
          {formatDateTime(v.started_at)} / {formatDateTime(v.finished_at)} (
          {formatDuration(v.duration_ms)})
        </dd>
      </dl>
      <p className="text-xs text-slate-500">
        VERIFIED needs every check to pass and full coverage. Scheduled, manual-review and
        unresolved items are never verified in the database, so they cap the outcome at PARTIAL.
      </p>

      <h3 className="font-medium">Checks ({v.checks.length})</h3>
      {v.checks.length === 0 ? (
        <EmptyState title="No checks were returned" />
      ) : (
        <table className="table" aria-label="Verification checks">
          <thead>
            <tr>
              <th scope="col">Result</th>
              <th scope="col">Check</th>
              <th scope="col">Target</th>
              <th scope="col">Step</th>
              <th scope="col">Observed</th>
              <th scope="col">Expected</th>
            </tr>
          </thead>
          <tbody>
            {v.checks.map((c, i) => (
              <tr key={`${c.check_type}-${c.step_order ?? "x"}-${i}`}>
                <td>
                  <PassFail passed={c.passed} />
                </td>
                <td className="font-mono text-xs">{c.check_type}</td>
                <td className="font-mono text-xs">
                  {[c.schema_name, c.table_name, c.column_name].filter(Boolean).join(".") || "-"}
                </td>
                <td>{c.step_order ?? "-"}</td>
                <td>
                  <pre className="max-w-xs overflow-auto text-[11px]">{prettyJson(c.observed)}</pre>
                </td>
                <td>
                  <pre className="max-w-xs overflow-auto text-[11px]">{prettyJson(c.expected)}</pre>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3 className="font-medium">Residual hits ({v.residual_hits.length})</h3>
      {v.residual_hits.length === 0 ? (
        <p className="text-sm text-slate-600">No residual hits were reported.</p>
      ) : (
        <table className="table" aria-label="Residual hits">
          <thead>
            <tr>
              <th scope="col">Table</th>
              <th scope="col">Column</th>
              <th scope="col">Count</th>
              <th scope="col">Raw</th>
            </tr>
          </thead>
          <tbody>
            {v.residual_hits.map((h, i) => {
              const r = residualRow(h);
              return (
                <tr key={i}>
                  <td className="font-mono text-xs">{r.table ?? "-"}</td>
                  <td className="font-mono text-xs">{r.column ?? "-"}</td>
                  <td>{formatInt(r.count)}</td>
                  <td>
                    <pre className="text-[11px]">{prettyJson(r.raw)}</pre>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ audit

export function ChainStatus({ chain }: { chain: ChainVerificationOut }) {
  return (
    <div className="space-y-1 text-sm" aria-label="Hash chain status">
      <p>
        Hash chain:{" "}
        {chain.valid ? (
          <Pill tone="green" symbol={"✓"}>
            VALID
          </Pill>
        ) : (
          <Pill tone="red" symbol={"✗"}>
            INVALID
          </Pill>
        )}{" "}
        ({formatInt(chain.checked_events)} events checked)
      </p>
      {chain.head_seq !== null ? (
        <p>
          Head seq {chain.head_seq}: <code className="break-all text-xs">{chain.head_hash}</code>
        </p>
      ) : null}
      {!chain.valid ? (
        <p className="text-red-800">
          First invalid seq: {chain.first_invalid_seq ?? "-"}
          {chain.reason ? `: ${chain.reason}` : ""}
        </p>
      ) : null}
    </div>
  );
}

function VerifyChainButton() {
  const vc = useVerifyChain();
  return (
    <div className="space-y-2">
      <button
        type="button"
        className="btn-secondary"
        onClick={() => vc.mutate()}
        disabled={vc.isPending}
      >
        {vc.isPending ? "Verifying chain..." : "Verify full audit chain"}
      </button>
      {vc.error ? <ErrorState error={vc.error} title="Chain verification failed" /> : null}
      {vc.data ? <ChainStatus chain={vc.data} /> : null}
    </div>
  );
}

function EvidencePanel({ ev, requestId }: { ev: EvidenceOut; requestId: string }) {
  const dl = useDownloadEvidence();
  const leaves = useDownloadEvidenceLeaves();
  return (
    <section className="card space-y-2 text-sm" aria-labelledby="ev-h">
      <h2 id="ev-h" className="section-title flex items-center gap-2">
        Evidence{" "}
        {ev.signature_valid ? (
          <Pill tone="green" symbol={"✓"}>
            Signature valid
          </Pill>
        ) : (
          <Pill tone="red" symbol={"✗"}>
            Signature INVALID
          </Pill>
        )}
      </h2>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1">
        <dt className="text-slate-500">Evidence id</dt>
        <dd className="font-mono text-xs">{ev.id}</dd>
        <dt className="text-slate-500">Issued</dt>
        <dd>{formatDateTime(ev.issued_at)}</dd>
        <dt className="text-slate-500">Leaves</dt>
        <dd>{formatInt(ev.leaf_count)}</dd>
        <dt className="text-slate-500">Chain anchor</dt>
        <dd>
          seq {ev.chain_anchor_seq}, <code className="text-xs">{shortHash(ev.chain_anchor_hash, 16)}</code>
        </dd>
        <dt className="text-slate-500">Signing key</dt>
        <dd className="font-mono text-xs">{ev.signing_key_id}</dd>
      </dl>
      <HashBadge label="merkle_root" value={ev.merkle_root} />
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          className="btn-secondary"
          disabled={dl.isPending}
          onClick={() =>
            dl.mutate(requestId, { onSuccess: (b) => saveBlob(b, `evidence-${requestId}.json`) })
          }
        >
          Download evidence bundle
        </button>
        <button
          type="button"
          className="btn-secondary"
          disabled={leaves.isPending}
          onClick={() =>
            leaves.mutate(requestId, { onSuccess: (b) => saveBlob(b, `leaves-${requestId}.json`) })
          }
        >
          Download Merkle leaves
        </button>
      </div>
      {dl.error ? <ErrorState error={dl.error} title="Evidence download failed" /> : null}
      {leaves.error ? <ErrorState error={leaves.error} title="Leaves download failed" /> : null}
      <p className="text-xs text-slate-500">
        Verify offline with <code>lae verify-evidence evidence.json --public-key ...</code>.
      </p>
    </section>
  );
}

export function EventsTable({ events }: { events: AuditEventOut[] }) {
  if (events.length === 0) return <EmptyState title="No audit events" />;
  return (
    <div className="overflow-x-auto">
      <table className="table" aria-label="Audit events">
        <thead>
          <tr>
            <th scope="col">Seq</th>
            <th scope="col">Time</th>
            <th scope="col">Event</th>
            <th scope="col">Entity</th>
            <th scope="col">Actor</th>
            <th scope="col">Event hash</th>
            <th scope="col">Payload</th>
          </tr>
        </thead>
        <tbody>
          {events.map((e) => (
            <tr key={e.seq}>
              <td>{e.seq}</td>
              <td className="whitespace-nowrap">{formatDateTime(e.occurred_at)}</td>
              <td className="font-mono text-xs">{e.event_type}</td>
              <td className="font-mono text-xs">
                {e.entity_type}
                {e.entity_id ? `:${e.entity_id.slice(0, 8)}` : ""}
              </td>
              <td>{e.actor}</td>
              <td className="font-mono text-xs" title={`prev ${e.prev_hash}`}>
                {shortHash(e.event_hash, 16)}
              </td>
              <td>
                <details>
                  <summary className="cursor-pointer text-xs">payload</summary>
                  <pre className="max-w-md overflow-auto text-[11px]">{prettyJson(e.payload)}</pre>
                </details>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ForRequest({ requestId }: { requestId: string }) {
  const ver = useVerification(requestId);
  const audit = useAuditForRequest(requestId);
  const run = useRunVerification(requestId);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          className="btn-secondary"
          disabled={run.isPending}
          onClick={() => run.mutate()}
        >
          {run.isPending ? "Running verification..." : "Re-run verification"}
        </button>
        <VerifyChainButton />
      </div>
      {run.error ? <ErrorState error={run.error} title="Verification run failed" /> : null}

      {ver.error ? (
        isNotFound(ver.error) ? (
          <EmptyState title="No verification run for this request yet">
            Verification runs after a LIVE execution, or with Re-run verification.
          </EmptyState>
        ) : (
          <ErrorState error={ver.error} title="Could not load verification" onRetry={() => void ver.refetch()} />
        )
      ) : ver.data ? (
        <VerificationSummary v={ver.data} />
      ) : (
        <Loading label="Loading verification" />
      )}

      {audit.error ? (
        <ErrorState error={audit.error} title="Could not load audit trail" onRetry={() => void audit.refetch()} />
      ) : audit.data ? (
        <>
          <section className="card" aria-labelledby="chain-h">
            <h2 id="chain-h" className="section-title">
              Audit chain (as reported for this request)
            </h2>
            <ChainStatus chain={audit.data.chain} />
          </section>
          {audit.data.evidence ? (
            <EvidencePanel ev={audit.data.evidence} requestId={requestId} />
          ) : (
            <EmptyState title="No evidence has been issued for this request" />
          )}
          <section className="space-y-2" aria-labelledby="events-h">
            <h2 id="events-h" className="section-title">
              Audit events for this request ({audit.data.events.length})
            </h2>
            <EventsTable events={audit.data.events} />
          </section>
        </>
      ) : (
        <Loading label="Loading audit trail" />
      )}
    </div>
  );
}

function GlobalAudit() {
  const events = useAuditEvents(0, 200);
  return (
    <div className="space-y-4">
      <VerifyChainButton />
      <section className="space-y-2" aria-labelledby="all-events-h">
        <h2 id="all-events-h" className="section-title">
          Audit events (first 200)
        </h2>
        {events.error ? (
          <ErrorState error={events.error} title="Could not load audit events" onRetry={() => void events.refetch()} />
        ) : events.data ? (
          <EventsTable events={events.data} />
        ) : (
          <Loading />
        )}
      </section>
    </div>
  );
}

export function VerificationPage() {
  const { requestId } = useParams();
  return (
    <div className="space-y-4">
      <h1 className="page-title">Verification &amp; Audit</h1>
      <RequestPicker
        basePath="/verification"
        value={requestId}
        allowNone
        noneLabel="(no request: global audit log)"
      />
      {requestId ? <ForRequest requestId={requestId} /> : <GlobalAudit />}
    </div>
  );
}
