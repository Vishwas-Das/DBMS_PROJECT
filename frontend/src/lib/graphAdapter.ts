/**
 * Pure adapters from the API graph shapes (SchemaGraphOut / LineageOut) to React Flow
 * nodes and edges, plus a deterministic dagre left-to-right layout.
 *
 * The three lineage levels stay separate (CLAUDE.md rule 4): the schema adapter never
 * produces record nodes, and the record adapter uses only LineageOut.records and record_edges.
 */
import { Graph, layout } from "@dagrejs/dagre";
import type { Edge, Node } from "@xyflow/react";
import type {
  EdgeOrigin,
  EdgeStatus,
  GraphEdgeOut,
  GraphNodeOut,
  LineageOut,
  RecordNodeOut,
  RecordRole,
  SchemaGraphOut,
  Traceability,
} from "../api/types";
import { ORIGIN_LABEL, ROLE_LABEL, TRACEABILITY_SHORT, shortHash } from "./format";

// ------------------------------------------------------------------ styling (pure data)

export interface EdgeVisual {
  /** SVG stroke-dasharray. Undefined means a solid line. */
  dash: string | undefined;
  stroke: string;
  /** Text pattern name, so colour is never the only signal. */
  pattern: "solid" | "dashed" | "dash-dot" | "dotted";
  label: string;
}

const ORIGIN_VISUAL: Record<EdgeOrigin, Omit<EdgeVisual, "label">> = {
  DECLARED_FK: { dash: undefined, stroke: "#334155", pattern: "solid" },
  SQL_LINEAGE: { dash: "8 5", stroke: "#1d4ed8", pattern: "dashed" },
  REGISTERED_MAPPING: { dash: "10 4 2 4", stroke: "#7c3aed", pattern: "dash-dot" },
  INFERRED: { dash: "2 5", stroke: "#ea580c", pattern: "dotted" },
};

export function edgeVisual(
  origin: EdgeOrigin,
  traceability: Traceability,
  status: EdgeStatus,
): EdgeVisual {
  const base = ORIGIN_VISUAL[origin];
  const suffix = status === "CONFIRMED" ? "" : ` (${status.toLowerCase()})`;
  return { ...base, label: `${ORIGIN_LABEL[origin]} | ${TRACEABILITY_SHORT[traceability]}${suffix}` };
}

export const EDGE_LEGEND: ReadonlyArray<{ origin: EdgeOrigin } & Omit<EdgeVisual, "label">> = (
  Object.keys(ORIGIN_VISUAL) as EdgeOrigin[]
).map((origin) => ({ origin, ...ORIGIN_VISUAL[origin] }));

/** Record roles: each role has a distinct shape and a text tag, not only a colour. */
export interface RoleVisual {
  shape: "double-box" | "box" | "dashed-box" | "pill";
  tag: string;
  fill: string;
  border: string;
}

export const ROLE_VISUAL: Record<RecordRole, RoleVisual> = {
  SUBJECT_ROOT: { shape: "double-box", tag: "ROOT", fill: "#fee2e2", border: "#b91c1c" },
  OWNED: { shape: "box", tag: "OWNED", fill: "#ffedd5", border: "#c2410c" },
  REFERENCING_OTHER_SUBJECT: {
    shape: "dashed-box",
    tag: "OTHER SUBJECT",
    fill: "#e0f2fe",
    border: "#0369a1",
  },
  SHARED: { shape: "pill", tag: "SHARED", fill: "#ede9fe", border: "#6d28d9" },
};

// ------------------------------------------------------------------ node / edge data

export interface SchemaNodeData extends Record<string, unknown> {
  kind: "table" | "column";
  title: string;
  subtitle: string;
  isArtifact: boolean;
  piiCategory: string | null;
  tableKind: string | null;
  source: GraphNodeOut;
}

export interface SchemaEdgeData extends Record<string, unknown> {
  edge: GraphEdgeOut;
  visual: EdgeVisual;
}

export interface RecordNodeData extends Record<string, unknown> {
  role: RecordRole;
  roleLabel: string;
  visual: RoleVisual;
  table: string;
  keyHmacShort: string;
  depth: number;
  isPredicate: boolean;
  source: RecordNodeOut;
}

export interface RecordEdgeData extends Record<string, unknown> {
  edgeId: string | null;
}

export type SchemaFlowNode = Node<SchemaNodeData, "schema">;
export type SchemaFlowEdge = Edge<SchemaEdgeData>;
export type RecordFlowNode = Node<RecordNodeData, "record">;
export type RecordFlowEdge = Edge<RecordEdgeData>;

export const NODE_SIZE = {
  table: { width: 200, height: 56 },
  column: { width: 220, height: 48 },
  record: { width: 200, height: 64 },
} as const;

function byId<T extends { id: string }>(a: T, b: T): number {
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}

function nodeTitle(n: GraphNodeOut): { title: string; subtitle: string } {
  const fq = `${n.schema_name}.${n.table_name}`;
  if (n.kind === "column" && n.column_name) return { title: n.column_name, subtitle: fq };
  return { title: fq, subtitle: n.table_kind ?? (n.is_artifact ? "artifact" : "table") };
}

export interface SchemaFlowOptions {
  /** Hide edges whose status is REJECTED (default true). SUGGESTED edges are always shown, styled. */
  hideRejected?: boolean;
  /** Hide nodes that no visible edge touches (default false). Used for the large column view. */
  hideIsolated?: boolean;
}

export function schemaGraphToFlow(
  graph: SchemaGraphOut,
  opts: SchemaFlowOptions = {},
): { nodes: SchemaFlowNode[]; edges: SchemaFlowEdge[] } {
  const hideRejected = opts.hideRejected ?? true;
  const known = new Set(graph.nodes.map((n) => n.id));
  const visibleEdges = graph.edges
    .filter((e) => known.has(e.source) && known.has(e.target))
    .filter((e) => !(hideRejected && e.status === "REJECTED"));
  const touched = new Set(visibleEdges.flatMap((e) => [e.source, e.target]));
  const keepNode = (n: GraphNodeOut) => !opts.hideIsolated || touched.has(n.id);
  const nodes: SchemaFlowNode[] = [...graph.nodes].filter(keepNode).sort(byId).map((n) => {
    const { title, subtitle } = nodeTitle(n);
    const size = n.kind === "column" ? NODE_SIZE.column : NODE_SIZE.table;
    return {
      id: n.id,
      type: "schema",
      position: { x: 0, y: 0 },
      width: size.width,
      height: size.height,
      data: {
        kind: n.kind,
        title,
        subtitle,
        isArtifact: n.is_artifact ?? false,
        piiCategory: n.pii_category ?? null,
        tableKind: n.table_kind ?? null,
        source: n,
      },
    };
  });
  const edges: SchemaFlowEdge[] = [...visibleEdges]
    .sort(byId)
    .map((e) => {
      const visual = edgeVisual(e.origin, e.traceability, e.status);
      return {
        id: e.id,
        source: e.source,
        target: e.target,
        label: visual.label,
        ariaLabel: `${visual.label} edge from ${e.source} to ${e.target}`,
        style: {
          stroke: visual.stroke,
          strokeWidth: 1.8,
          ...(visual.dash ? { strokeDasharray: visual.dash } : {}),
        },
        labelStyle: { fontSize: 10, fill: visual.stroke },
        data: { edge: e, visual },
      };
    });
  return { nodes, edges };
}

export function recordGraphToFlow(lineage: LineageOut): {
  nodes: RecordFlowNode[];
  edges: RecordFlowEdge[];
} {
  const known = new Set(lineage.records.map((r) => r.id));
  const nodes: RecordFlowNode[] = [...lineage.records].sort(byId).map((r) => ({
    id: r.id,
    type: "record",
    position: { x: 0, y: 0 },
    width: NODE_SIZE.record.width,
    height: NODE_SIZE.record.height,
    data: {
      role: r.role,
      roleLabel: ROLE_LABEL[r.role],
      visual: ROLE_VISUAL[r.role],
      table: r.table,
      keyHmacShort: shortHash(r.id, 10),
      depth: r.depth,
      isPredicate: r.is_predicate ?? false,
      source: r,
    },
  }));
  const edges: RecordFlowEdge[] = lineage.record_edges
    .filter((e) => known.has(e.source) && known.has(e.target))
    .map((e) => ({
      id: `${e.source}->${e.target}#${e.edge_id ?? "none"}`,
      source: e.source,
      target: e.target,
      style: { stroke: "#475569", strokeWidth: 1.4 },
      data: { edgeId: e.edge_id },
    }))
    .sort(byId);
  return { nodes, edges };
}

/** Number of nodes that no non-rejected edge touches (display only). */
export function isolatedNodeCount(graph: SchemaGraphOut): number {
  const all = schemaGraphToFlow(graph).nodes.length;
  return all - schemaGraphToFlow(graph, { hideIsolated: true }).nodes.length;
}

/** INFERRED edges for human review, SUGGESTED first, then by source/target/id. */
export function inferredEdgesForReview(graph: SchemaGraphOut): GraphEdgeOut[] {
  const rank = (e: GraphEdgeOut) => (e.status === "SUGGESTED" ? 0 : e.status === "CONFIRMED" ? 1 : 2);
  return graph.edges
    .filter((e) => e.origin === "INFERRED")
    .sort(
      (a, b) =>
        rank(a) - rank(b) ||
        a.source.localeCompare(b.source) ||
        a.target.localeCompare(b.target) ||
        (a.id < b.id ? -1 : a.id > b.id ? 1 : 0),
    );
}

/** Counts records per role (display only). */
export function countByRole(records: readonly RecordNodeOut[]): Record<RecordRole, number> {
  const out: Record<RecordRole, number> = {
    SUBJECT_ROOT: 0,
    OWNED: 0,
    REFERENCING_OTHER_SUBJECT: 0,
    SHARED: 0,
  };
  for (const r of records) out[r.role] += 1;
  return out;
}

/** Edge lookup by id across any number of schema graphs (used to resolve reason_edge_ids). */
export function indexEdges(
  ...graphs: ReadonlyArray<SchemaGraphOut | undefined>
): Map<string, GraphEdgeOut> {
  const m = new Map<string, GraphEdgeOut>();
  for (const g of graphs) {
    if (!g) continue;
    for (const e of g.edges) if (!m.has(e.id)) m.set(e.id, e);
  }
  return m;
}

// ------------------------------------------------------------------ layout

export interface LayoutOptions {
  rankdir?: "LR" | "TB";
  nodesep?: number;
  ranksep?: number;
}

/**
 * Deterministic dagre layout (input sorted by id). Returns new node objects positioned
 * at their top-left corner. Input arrays are not mutated.
 */
export function layoutGraph<N extends Node, E extends Edge>(
  nodes: readonly N[],
  edges: readonly E[],
  opts: LayoutOptions = {},
): N[] {
  const g = new Graph<object, { width: number; height: number; x?: number; y?: number }>();
  g.setGraph({
    rankdir: opts.rankdir ?? "LR",
    nodesep: opts.nodesep ?? 30,
    ranksep: opts.ranksep ?? 90,
    marginx: 10,
    marginy: 10,
  });
  g.setDefaultEdgeLabel(() => ({}));
  for (const n of nodes) {
    g.setNode(n.id, { width: n.width ?? 180, height: n.height ?? 50 });
  }
  for (const e of edges) {
    if (g.hasNode(e.source) && g.hasNode(e.target)) g.setEdge(e.source, e.target);
  }
  layout(g);
  return nodes.map((n) => {
    const p = g.node(n.id);
    const w = n.width ?? 180;
    const h = n.height ?? 50;
    const x = p?.x ?? 0;
    const y = p?.y ?? 0;
    return { ...n, position: { x: x - w / 2, y: y - h / 2 } };
  });
}
