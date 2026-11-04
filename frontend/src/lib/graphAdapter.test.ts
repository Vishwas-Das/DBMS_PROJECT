import { describe, expect, it } from "vitest";
import type { GraphEdgeOut, GraphNodeOut, LineageOut, SchemaGraphOut } from "../api/types";
import {
  EDGE_LEGEND,
  ROLE_VISUAL,
  countByRole,
  edgeVisual,
  indexEdges,
  layoutGraph,
  recordGraphToFlow,
  schemaGraphToFlow,
} from "./graphAdapter";

// Test-only fixtures. The shipped app never contains sample data.
const node = (id: string, extra: Partial<GraphNodeOut> = {}): GraphNodeOut => ({
  id,
  kind: "table",
  schema_name: "app",
  table_name: id.split(".")[1] ?? id,
  column_name: null,
  table_kind: "TABLE",
  pii_category: null,
  is_artifact: false,
  ...extra,
});

const edge = (id: string, source: string, target: string, extra: Partial<GraphEdgeOut> = {}): GraphEdgeOut => ({
  id,
  source,
  target,
  level: "TABLE",
  origin: "DECLARED_FK",
  transform: "FOREIGN_KEY",
  traceability: "KEY_PRESERVING",
  status: "CONFIRMED",
  source_columns: ["id"],
  target_columns: ["customer_id"],
  on_delete_action: "CASCADE",
  confidence: 1,
  evidence: { constraint: "fk_order_customer_id_customer" },
  ...extra,
});

const graph: SchemaGraphOut = {
  snapshot_id: "00000000-0000-0000-0000-000000000001",
  level: "TABLE",
  nodes: [node("app.order"), node("app.customer"), node("app.order_summary", { is_artifact: true })],
  edges: [
    edge("e2", "app.order", "app.order_summary", {
      origin: "SQL_LINEAGE",
      transform: "AGGREGATE",
      traceability: "AGGREGATE",
    }),
    edge("e1", "app.customer", "app.order"),
    edge("e3", "app.customer", "app.ghost"),
    edge("e4", "app.customer", "app.order_summary", { origin: "INFERRED", status: "SUGGESTED" }),
    edge("e5", "app.customer", "app.order_summary", { origin: "INFERRED", status: "REJECTED" }),
  ],
};

describe("edgeVisual", () => {
  it("gives every origin a distinct dash pattern", () => {
    const dashes = EDGE_LEGEND.map((l) => l.dash ?? "solid");
    expect(new Set(dashes).size).toBe(4);
    expect(edgeVisual("DECLARED_FK", "KEY_PRESERVING", "CONFIRMED").dash).toBeUndefined();
    expect(edgeVisual("SQL_LINEAGE", "AGGREGATE", "CONFIRMED").pattern).toBe("dashed");
    expect(edgeVisual("REGISTERED_MAPPING", "KEY_PRESERVING", "CONFIRMED").pattern).toBe("dash-dot");
    const inf = edgeVisual("INFERRED", "OPAQUE", "SUGGESTED");
    expect(inf.pattern).toBe("dotted");
    expect(inf.stroke).toBe("#ea580c");
  });

  it("puts origin, traceability and non-confirmed status in the label text", () => {
    expect(edgeVisual("DECLARED_FK", "KEY_PRESERVING", "CONFIRMED").label).toBe("FK | KEY");
    expect(edgeVisual("SQL_LINEAGE", "AGGREGATE", "CONFIRMED").label).toBe("SQL | AGG");
    expect(edgeVisual("INFERRED", "OPAQUE", "SUGGESTED").label).toBe("Inferred | OPAQUE (suggested)");
  });
});

describe("schemaGraphToFlow", () => {
  const flow = schemaGraphToFlow(graph);

  it("maps nodes sorted by id with titles and artifact flag", () => {
    expect(flow.nodes.map((n) => n.id)).toEqual(["app.customer", "app.order", "app.order_summary"]);
    const summary = flow.nodes.find((n) => n.id === "app.order_summary");
    expect(summary?.data.isArtifact).toBe(true);
    expect(summary?.data.title).toBe("app.order_summary");
    expect(summary?.type).toBe("schema");
  });

  it("drops dangling and rejected edges, keeps suggested ones", () => {
    expect(flow.edges.map((e) => e.id)).toEqual(["e1", "e2", "e4"]);
    expect(schemaGraphToFlow(graph, { hideRejected: false }).edges.map((e) => e.id)).toContain("e5");
  });

  it("styles edges by origin and keeps the server edge in data", () => {
    const fk = flow.edges.find((e) => e.id === "e1");
    const sql = flow.edges.find((e) => e.id === "e2");
    expect(fk?.style?.strokeDasharray).toBeUndefined();
    expect(sql?.style?.strokeDasharray).toBe("8 5");
    expect(sql?.label).toBe("SQL | AGG");
    expect(fk?.data?.edge.evidence).toEqual({ constraint: "fk_order_customer_id_customer" });
  });

  it("labels column nodes with the column and the owning table", () => {
    const col = schemaGraphToFlow({
      ...graph,
      level: "COLUMN",
      nodes: [node("app.customer.email", { kind: "column", table_name: "customer", column_name: "email", pii_category: "EMAIL" })],
      edges: [],
    });
    expect(col.nodes[0]?.data.title).toBe("email");
    expect(col.nodes[0]?.data.subtitle).toBe("app.customer");
    expect(col.nodes[0]?.data.piiCategory).toBe("EMAIL");
  });

  it("does not mutate its input", () => {
    const before = JSON.stringify(graph);
    schemaGraphToFlow(graph);
    expect(JSON.stringify(graph)).toBe(before);
  });
});

describe("recordGraphToFlow", () => {
  const lineage: LineageOut = {
    request_id: "00000000-0000-0000-0000-0000000000aa",
    plan_id: null,
    snapshot_id: graph.snapshot_id,
    schema_graph: graph,
    records: [
      { id: "h_root", table: "app.customer", role: "SUBJECT_ROOT", depth: 0, is_predicate: false },
      { id: "h_o1", table: "app.order", role: "OWNED", depth: 1, is_predicate: false },
      { id: "h_t1", table: "app.ticket", role: "REFERENCING_OTHER_SUBJECT", depth: 1, is_predicate: true },
      { id: "h_s1", table: "app.household", role: "SHARED", depth: 2, is_predicate: false },
    ],
    record_edges: [
      { source: "h_root", target: "h_o1", edge_id: "e1" },
      { source: "h_root", target: "h_t1", edge_id: null },
      { source: "h_o1", target: "h_missing", edge_id: "e1" },
    ],
  };

  it("maps each role to a distinct shape and text tag", () => {
    const { nodes } = recordGraphToFlow(lineage);
    const shapes = new Set(Object.values(ROLE_VISUAL).map((v) => v.shape));
    const tags = new Set(Object.values(ROLE_VISUAL).map((v) => v.tag));
    expect(shapes.size).toBe(4);
    expect(tags.size).toBe(4);
    const root = nodes.find((n) => n.id === "h_root");
    expect(root?.data.visual.tag).toBe("ROOT");
    expect(root?.type).toBe("record");
    expect(nodes.find((n) => n.id === "h_t1")?.data.isPredicate).toBe(true);
  });

  it("keeps only edges between known records", () => {
    const { edges } = recordGraphToFlow(lineage);
    expect(edges).toHaveLength(2);
    expect(edges.map((e) => e.data?.edgeId).sort()).toEqual(["e1", null].sort());
  });

  it("counts records by role", () => {
    expect(countByRole(lineage.records)).toEqual({
      SUBJECT_ROOT: 1,
      OWNED: 1,
      REFERENCING_OTHER_SUBJECT: 1,
      SHARED: 1,
    });
  });
});

describe("indexEdges", () => {
  it("indexes edges across graphs and skips undefined", () => {
    const m = indexEdges(graph, undefined, { ...graph, edges: [edge("c1", "a", "b", { level: "COLUMN" })] });
    expect(m.get("e1")?.source).toBe("app.customer");
    expect(m.get("c1")?.level).toBe("COLUMN");
    expect(m.size).toBe(6);
  });
});

describe("layoutGraph", () => {
  it("lays out left to right deterministically without mutating input", () => {
    const flow = schemaGraphToFlow(graph);
    const before = flow.nodes.map((n) => ({ ...n.position }));
    const a = layoutGraph(flow.nodes, flow.edges);
    const b = layoutGraph(flow.nodes, flow.edges);
    expect(a.map((n) => n.position)).toEqual(b.map((n) => n.position));
    expect(flow.nodes.map((n) => n.position)).toEqual(before);
    const x = (id: string) => a.find((n) => n.id === id)?.position.x ?? NaN;
    expect(x("app.customer")).toBeLessThan(x("app.order"));
    expect(x("app.order")).toBeLessThan(x("app.order_summary"));
  });

  it("supports top-to-bottom and an empty graph", () => {
    const flow = schemaGraphToFlow(graph);
    const tb = layoutGraph(flow.nodes, flow.edges, { rankdir: "TB" });
    const y = (id: string) => tb.find((n) => n.id === id)?.position.y ?? NaN;
    expect(y("app.customer")).toBeLessThan(y("app.order"));
    expect(layoutGraph([], [])).toEqual([]);
  });
});
