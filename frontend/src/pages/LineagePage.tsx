import { useState } from "react";
import { useParams } from "react-router-dom";
import { isApiError } from "../api/client";
import { useLineage, useSchemaGraph } from "../api/hooks";
import type { GraphEdgeOut, GraphLevel, SchemaGraphOut } from "../api/types";
import { EdgeStatusBadge, TraceabilityBadge } from "../components/Badges";
import { EdgeEvidencePanel } from "../components/EdgeEvidencePanel";
import { EdgeReview, EdgeReviewHelp } from "../components/EdgeReview";
import { EdgeLegend, RecordGraphView, RoleLegend, SchemaGraphView } from "../components/GraphView";
import { RequestPicker } from "../components/RequestPicker";
import { EmptyState, QueryState } from "../components/States";
import { ORIGIN_LABEL, ROLE_LABEL, evidenceSummary, formatInt, nodeLabel, shortHash } from "../lib/format";
import { countByRole, inferredEdgesForReview, isolatedNodeCount } from "../lib/graphAdapter";
import type { LineageOut, RecordRole } from "../api/types";

type View = "schema" | "records";

function ToggleGroup<T extends string>({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: ReadonlyArray<{ value: T; label: string; disabled?: boolean }>;
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div role="group" aria-label={label} className="inline-flex rounded border border-slate-300">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          aria-pressed={value === o.value}
          disabled={o.disabled}
          onClick={() => onChange(o.value)}
          className={`px-3 py-1.5 text-sm ${
            value === o.value ? "bg-slate-800 font-semibold text-white" : "bg-white hover:bg-slate-100"
          } disabled:opacity-40`}
        >
          {value === o.value ? "● " : ""}
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** Text alternative to the graph: every edge is listed and keyboard-selectable. */
function EdgeTable({
  graph,
  onSelect,
}: {
  graph: SchemaGraphOut;
  onSelect: (e: GraphEdgeOut) => void;
}) {
  if (graph.edges.length === 0) return <EmptyState title="This graph has no edges" />;
  return (
    <details className="card">
      <summary className="cursor-pointer text-sm font-medium">
        Edge list ({graph.edges.length}), a text alternative to the graph
      </summary>
      <div className="mt-2 max-h-96 overflow-auto">
      <table className="table">
        <thead>
          <tr>
            <th scope="col">Source</th>
            <th scope="col">Target</th>
            <th scope="col">Origin</th>
            <th scope="col">Traceability</th>
            <th scope="col">Status</th>
            <th scope="col">Evidence</th>
          </tr>
        </thead>
        <tbody>
          {graph.edges.map((e) => (
            <tr key={e.id}>
              <td className="font-mono text-xs">{nodeLabel(e.source)}</td>
              <td className="font-mono text-xs">{nodeLabel(e.target)}</td>
              <td>{ORIGIN_LABEL[e.origin]}</td>
              <td>
                <TraceabilityBadge traceability={e.traceability} />
              </td>
              <td>
                <EdgeStatusBadge status={e.status} />
              </td>
              <td>
                <button type="button" className="link" onClick={() => onSelect(e)}>
                  Show evidence
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
    </details>
  );
}

/** Review queue for INFERRED edges (confirm / reject), with the evidence that produced them. */
function SuggestionsSection({
  graph,
  onSelect,
}: {
  graph: SchemaGraphOut;
  onSelect: (e: GraphEdgeOut) => void;
}) {
  const inferred = inferredEdgesForReview(graph);
  const open = inferred.filter((e) => e.status === "SUGGESTED").length;
  return (
    <section className="card space-y-2" aria-labelledby="suggest-h">
      <h2 id="suggest-h" className="section-title">
        Suggested edges awaiting review ({open} of {inferred.length} inferred)
      </h2>
      <EdgeReviewHelp />
      {inferred.length === 0 ? (
        <p className="text-sm text-slate-500">This snapshot has no inferred edges at this level.</p>
      ) : (
        <div className="max-h-96 overflow-auto">
          <table className="table" aria-label="Inferred edges">
            <thead>
              <tr>
                <th scope="col">Source &rarr; target</th>
                <th scope="col">Columns</th>
                <th scope="col">Evidence</th>
                <th scope="col">Confidence</th>
                <th scope="col">Status</th>
                <th scope="col">Review</th>
              </tr>
            </thead>
            <tbody>
              {inferred.map((e) => {
                const ev = evidenceSummary(e.evidence);
                return (
                  <tr key={e.id}>
                    <td className="font-mono text-xs break-all">
                      {nodeLabel(e.source)} &rarr; {nodeLabel(e.target)}
                    </td>
                    <td className="font-mono text-xs">
                      ({e.source_columns.join(", ") || "-"}) &rarr; (
                      {e.target_columns.join(", ") || "-"})
                    </td>
                    <td className="text-xs">
                      {ev.method === "residual_scan" ? (
                        <>
                          residual scan: <strong>{formatInt(ev.hitCount)}</strong> hit(s)
                        </>
                      ) : ev.contained !== null && ev.sampled !== null ? (
                        <>
                          {ev.method ?? "sampled"}: {formatInt(ev.contained)}/
                          {formatInt(ev.sampled)} sampled values contained
                        </>
                      ) : (
                        (ev.method ?? "-")
                      )}
                    </td>
                    <td>{e.confidence}</td>
                    <td>
                      <EdgeStatusBadge status={e.status} />
                    </td>
                    <td>
                      <button
                        type="button"
                        className="btn-secondary"
                        onClick={() => onSelect(e)}
                        aria-label={`Review edge ${e.source} to ${e.target}`}
                      >
                        Review...
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function SchemaSection({
  graph,
  selectedId,
  setSelectedId,
}: {
  graph: SchemaGraphOut;
  selectedId: string | null;
  setSelectedId: (id: string | null) => void;
}) {
  // Column graphs of the dev DB have ~90 nodes, most without edges: hide those by default.
  const [showIsolated, setShowIsolated] = useState(graph.level === "TABLE");
  if (graph.nodes.length === 0) {
    return <EmptyState title="The snapshot has no nodes at this level" />;
  }
  // Looked up in the current graph, so a status change shows the refetched edge.
  const selected = selectedId ? (graph.edges.find((e) => e.id === selectedId) ?? null) : null;
  const isolated = isolatedNodeCount(graph);
  const select = (e: GraphEdgeOut | null) => setSelectedId(e?.id ?? null);
  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">
        Snapshot <code>{graph.snapshot_id}</code>, level {graph.level}: {graph.nodes.length} nodes,{" "}
        {graph.edges.length} edges. Click an edge (or use the edge list) to see its evidence. A
        table or column edge does not imply affected records.
      </p>
      <EdgeLegend />
      {isolated > 0 ? (
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={showIsolated}
            onChange={(e) => setShowIsolated(e.target.checked)}
          />
          Show {isolated} node(s) without lineage edges
        </label>
      ) : null}
      <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_400px]">
        <SchemaGraphView graph={graph} onSelectEdge={select} hideIsolated={!showIsolated} />
        {selected ? (
          <EdgeEvidencePanel
            edge={selected}
            onClose={() => setSelectedId(null)}
            actions={
              selected.origin === "INFERRED" ? (
                <EdgeReview key={selected.id} edge={selected} />
              ) : null
            }
          />
        ) : (
          <EmptyState title="No edge selected">Select an edge to inspect its evidence.</EmptyState>
        )}
      </div>
      <SuggestionsSection graph={graph} onSelect={select} />
      <EdgeTable graph={graph} onSelect={select} />
    </div>
  );
}

/** Text alternative for the record graph: one row per record (key HMAC truncated). */
function RecordTable({ lineage }: { lineage: LineageOut }) {
  const rows = [...lineage.records].sort(
    (a, b) => a.depth - b.depth || a.table.localeCompare(b.table) || a.id.localeCompare(b.id),
  );
  return (
    <details className="card">
      <summary className="cursor-pointer text-sm font-medium">
        Record list ({rows.length}), a text alternative to the graph
      </summary>
      <div className="mt-2 max-h-96 overflow-auto">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Depth</th>
              <th scope="col">Table</th>
              <th scope="col">Role</th>
              <th scope="col">Key HMAC</th>
              <th scope="col">Predicate</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.table}|${r.id}`}>
                <td>{r.depth}</td>
                <td className="font-mono text-xs">{r.table}</td>
                <td>{ROLE_LABEL[r.role]}</td>
                <td className="font-mono text-xs" title="HMAC of the record key, never the key">
                  {shortHash(r.id, 16)}
                </td>
                <td>{r.is_predicate ? "yes" : "no"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

export function LineagePage() {
  const { requestId } = useParams();
  const [level, setLevel] = useState<GraphLevel>("TABLE");
  const [view, setView] = useState<View>("schema");
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const schema = useSchemaGraph(level, !requestId);
  const lineage = useLineage(requestId, level);
  const effectiveView: View = requestId ? view : "schema";

  const changeLevel = (l: GraphLevel) => {
    setLevel(l);
    setSelectedId(null);
  };

  return (
    <div className="space-y-4">
      <h1 className="page-title">Lineage Explorer</h1>
      <RequestPicker
        basePath="/lineage"
        value={requestId}
        allowNone
        noneLabel="(no request: latest snapshot schema graph)"
      />
      <div className="flex flex-wrap items-center gap-3">
        <ToggleGroup<View>
          label="Lineage level"
          value={effectiveView}
          onChange={setView}
          options={[
            { value: "schema", label: "Schema graph" },
            { value: "records", label: "Record graph", disabled: !requestId },
          ]}
        />
        {effectiveView === "schema" ? (
          <ToggleGroup<GraphLevel>
            label="Schema granularity"
            value={level}
            onChange={changeLevel}
            options={[
              { value: "TABLE", label: "Table view" },
              { value: "COLUMN", label: "Column view" },
            ]}
          />
        ) : null}
        <span className="text-sm text-slate-600">
          Showing:{" "}
          <strong>
            {effectiveView === "records" ? "RECORD level" : `${level} level`}
          </strong>
        </span>
      </div>

      {!requestId ? (
        isApiError(schema.error) && schema.error.status === 404 ? (
          <EmptyState title="No lineage snapshot yet">
            The server has not captured a catalog snapshot ({schema.error.code}). Running discovery
            for a request (Erasure Plan page) creates one.
          </EmptyState>
        ) : (
          <QueryState query={schema} errorTitle="Could not load the schema graph">
            {(g) => (
              <SchemaSection
                key={g.level}
                graph={g}
                selectedId={selectedId}
                setSelectedId={setSelectedId}
              />
            )}
          </QueryState>
        )
      ) : (
        <QueryState query={lineage} errorTitle="Could not load lineage for this request">
          {(l) =>
            effectiveView === "schema" ? (
              <SchemaSection
                key={l.schema_graph.level}
                graph={l.schema_graph}
                selectedId={selectedId}
                setSelectedId={setSelectedId}
              />
            ) : l.records.length === 0 ? (
              <EmptyState title="No record-level lineage">
                Records come only from executing discovery queries. Run discovery for this request
                on the Erasure Plan page.
              </EmptyState>
            ) : (
              <div className="space-y-3">
                <RoleLegend />
                <RoleCounts counts={countByRole(l.records)} />
                <p className="text-xs text-slate-500">
                  Record nodes are identified by key HMAC (shown truncated:{" "}
                  {shortHash(l.records[0]?.id, 10)}), never by raw key values.
                </p>
                <RecordGraphView lineage={l} />
                <RecordTable lineage={l} />
              </div>
            )
          }
        </QueryState>
      )}
    </div>
  );
}

function RoleCounts({ counts }: { counts: Record<RecordRole, number> }) {
  return (
    <ul className="flex flex-wrap gap-4 text-sm" aria-label="Records by role">
      {(Object.keys(counts) as RecordRole[]).map((r) => (
        <li key={r}>
          {ROLE_LABEL[r]}: <strong>{counts[r]}</strong>
        </li>
      ))}
    </ul>
  );
}
