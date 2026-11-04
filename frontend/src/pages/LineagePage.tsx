import { useState } from "react";
import { useParams } from "react-router-dom";
import { useLineage, useSchemaGraph } from "../api/hooks";
import type { GraphEdgeOut, GraphLevel, SchemaGraphOut } from "../api/types";
import { EdgeStatusBadge, TraceabilityBadge } from "../components/Badges";
import { EdgeEvidencePanel } from "../components/EdgeEvidencePanel";
import { EdgeLegend, RecordGraphView, RoleLegend, SchemaGraphView } from "../components/GraphView";
import { RequestPicker } from "../components/RequestPicker";
import { EmptyState, QueryState } from "../components/States";
import { ORIGIN_LABEL, ROLE_LABEL, shortHash } from "../lib/format";
import { countByRole } from "../lib/graphAdapter";
import type { RecordRole } from "../api/types";

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
      <table className="table mt-2">
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
              <td className="font-mono text-xs">{e.source}</td>
              <td className="font-mono text-xs">{e.target}</td>
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
    </details>
  );
}

function SchemaSection({
  graph,
  selected,
  setSelected,
}: {
  graph: SchemaGraphOut;
  selected: GraphEdgeOut | null;
  setSelected: (e: GraphEdgeOut | null) => void;
}) {
  if (graph.nodes.length === 0) {
    return <EmptyState title="The snapshot has no nodes at this level" />;
  }
  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">
        Snapshot <code>{graph.snapshot_id}</code>, level {graph.level}: {graph.nodes.length} nodes,{" "}
        {graph.edges.length} edges. Click an edge (or use the edge list) to see its evidence. A
        table or column edge does not imply affected records.
      </p>
      <EdgeLegend />
      <div className="grid gap-3 xl:grid-cols-[1fr_380px]">
        <SchemaGraphView graph={graph} onSelectEdge={setSelected} />
        {selected ? (
          <EdgeEvidencePanel edge={selected} onClose={() => setSelected(null)} />
        ) : (
          <EmptyState title="No edge selected">Select an edge to inspect its evidence.</EmptyState>
        )}
      </div>
      <EdgeTable graph={graph} onSelect={setSelected} />
    </div>
  );
}

export function LineagePage() {
  const { requestId } = useParams();
  const [level, setLevel] = useState<GraphLevel>("TABLE");
  const [view, setView] = useState<View>("schema");
  const [selected, setSelected] = useState<GraphEdgeOut | null>(null);

  const schema = useSchemaGraph(level, !requestId);
  const lineage = useLineage(requestId, level);
  const effectiveView: View = requestId ? view : "schema";

  const changeLevel = (l: GraphLevel) => {
    setLevel(l);
    setSelected(null);
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
        <QueryState query={schema} errorTitle="Could not load the schema graph">
          {(g) => <SchemaSection graph={g} selected={selected} setSelected={setSelected} />}
        </QueryState>
      ) : (
        <QueryState query={lineage} errorTitle="Could not load lineage for this request">
          {(l) =>
            effectiveView === "schema" ? (
              <SchemaSection graph={l.schema_graph} selected={selected} setSelected={setSelected} />
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
