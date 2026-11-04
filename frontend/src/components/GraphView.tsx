import { useMemo } from "react";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type NodeProps,
  type OnSelectionChangeParams,
} from "@xyflow/react";
import type { GraphEdgeOut, LineageOut, SchemaGraphOut } from "../api/types";
import {
  EDGE_LEGEND,
  ROLE_VISUAL,
  layoutGraph,
  recordGraphToFlow,
  schemaGraphToFlow,
  type RecordFlowNode,
  type SchemaFlowNode,
} from "../lib/graphAdapter";
import { ORIGIN_LABEL, ROLE_LABEL, TRACEABILITY_LABEL, TRACEABILITY_SHORT } from "../lib/format";
import type { RecordRole, Traceability } from "../api/types";

// ------------------------------------------------------------------ custom nodes

function SchemaNode({ data }: NodeProps<SchemaFlowNode>) {
  const isCol = data.kind === "column";
  return (
    <div
      className={`h-full w-full rounded border bg-white px-2 py-1 text-xs shadow-sm ${
        data.isArtifact ? "border-violet-500 border-2" : "border-slate-400"
      }`}
    >
      <Handle type="target" position={Position.Left} />
      <div className="flex items-center justify-between gap-1">
        <span className="truncate font-semibold" title={data.title}>
          {isCol ? "col " : "tbl "}
          {data.title}
        </span>
        {data.piiCategory ? (
          <span className="rounded bg-rose-100 px-1 text-[10px] text-rose-900" title="PII category">
            PII:{data.piiCategory}
          </span>
        ) : null}
      </div>
      <div className="truncate text-slate-500" title={data.subtitle}>
        {data.isArtifact ? "[artifact] " : ""}
        {data.subtitle}
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function roleClass(shape: string): string {
  switch (shape) {
    case "double-box":
      return "rounded-sm border-4 border-double";
    case "dashed-box":
      return "rounded-sm border-2 border-dashed";
    case "pill":
      return "rounded-full border-2";
    default:
      return "rounded-sm border-2";
  }
}

function RecordNode({ data }: NodeProps<RecordFlowNode>) {
  const v = data.visual;
  return (
    <div
      className={`h-full w-full px-2 py-1 text-xs ${roleClass(v.shape)}`}
      style={{ background: v.fill, borderColor: v.border }}
      title={`${data.roleLabel} record in ${data.table}`}
    >
      <Handle type="target" position={Position.Left} />
      <div className="font-bold tracking-wide">[{v.tag}]</div>
      <div className="truncate">{data.table}</div>
      <div className="truncate font-mono text-[10px] text-slate-600">
        key#{data.keyHmacShort} d={data.depth}
        {data.isPredicate ? " predicate" : ""}
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const nodeTypes = { schema: SchemaNode, record: RecordNode };

// ------------------------------------------------------------------ legends

export function EdgeLegend() {
  const traces: Traceability[] = ["KEY_PRESERVING", "AGGREGATE", "OPAQUE"];
  return (
    <div className="flex flex-wrap gap-4 text-xs" aria-label="Edge legend">
      {EDGE_LEGEND.map((l) => (
        <span key={l.origin} className="inline-flex items-center gap-1">
          <svg width="36" height="8" aria-hidden="true">
            <line
              x1="0"
              y1="4"
              x2="36"
              y2="4"
              stroke={l.stroke}
              strokeWidth="2"
              strokeDasharray={l.dash}
            />
          </svg>
          {ORIGIN_LABEL[l.origin]} ({l.pattern})
        </span>
      ))}
      <span className="text-slate-500">
        Label suffix:{" "}
        {traces.map((t) => `${TRACEABILITY_SHORT[t]} = ${TRACEABILITY_LABEL[t]}`).join(", ")}
      </span>
    </div>
  );
}

export function RoleLegend() {
  return (
    <div className="flex flex-wrap gap-3 text-xs" aria-label="Record role legend">
      {(Object.keys(ROLE_VISUAL) as RecordRole[]).map((r) => {
        const v = ROLE_VISUAL[r];
        return (
          <span
            key={r}
            className={`px-2 py-0.5 ${roleClass(v.shape)}`}
            style={{ background: v.fill, borderColor: v.border }}
          >
            [{v.tag}] {ROLE_LABEL[r]}
          </span>
        );
      })}
    </div>
  );
}

// ------------------------------------------------------------------ views

export function SchemaGraphView({
  graph,
  onSelectEdge,
}: {
  graph: SchemaGraphOut;
  onSelectEdge: (e: GraphEdgeOut | null) => void;
}) {
  const { nodes, edges } = useMemo(() => {
    const flow = schemaGraphToFlow(graph);
    const withMarkers = flow.edges.map((e) => ({
      ...e,
      markerEnd: { type: MarkerType.ArrowClosed, color: e.data?.visual.stroke },
    }));
    return { nodes: layoutGraph(flow.nodes, withMarkers), edges: withMarkers };
  }, [graph]);

  const onSelection = ({ edges: sel }: OnSelectionChangeParams) => {
    const first = sel[0];
    if (first) onSelectEdge((first.data as { edge: GraphEdgeOut } | undefined)?.edge ?? null);
  };

  return (
    <div className="h-[560px] rounded border border-slate-300 bg-white">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        nodesConnectable={false}
        nodesDraggable
        edgesFocusable
        onEdgeClick={(_, e) => onSelectEdge(e.data?.edge ?? null)}
        onSelectionChange={onSelection}
        proOptions={{ hideAttribution: true }}
        minZoom={0.1}
      >
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}

export function RecordGraphView({ lineage }: { lineage: LineageOut }) {
  const { nodes, edges } = useMemo(() => {
    const flow = recordGraphToFlow(lineage);
    const withMarkers = flow.edges.map((e) => ({
      ...e,
      markerEnd: { type: MarkerType.ArrowClosed },
    }));
    return { nodes: layoutGraph(flow.nodes, withMarkers), edges: withMarkers };
  }, [lineage]);

  return (
    <div className="h-[560px] rounded border border-slate-300 bg-white">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        nodesConnectable={false}
        proOptions={{ hideAttribution: true }}
        minZoom={0.05}
      >
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
