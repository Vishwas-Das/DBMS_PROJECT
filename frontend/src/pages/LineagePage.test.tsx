import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setCredentials } from "../api/credentials";
import type { GraphEdgeOut, GraphNodeOut, SchemaGraphOut } from "../api/types";
import { installFetch, json, renderWithProviders } from "../test/render";
import { LineagePage } from "./LineagePage";

// Test-only fixtures. The shipped app never contains sample data.
const SNAP = "33333333-3333-3333-3333-333333333333";
const RESIDUAL = "aaaaaaaa-0000-0000-0000-000000000001";

const tableNode = (t: string): GraphNodeOut => ({
  id: `t:app.${t}`,
  kind: "table",
  schema_name: "app",
  table_name: t,
  column_name: null,
  table_kind: "TABLE",
  pii_category: null,
  is_artifact: false,
});

const residualEdge = (status: GraphEdgeOut["status"]): GraphEdgeOut => ({
  id: RESIDUAL,
  source: "t:app.customer",
  target: "t:app.document",
  level: "TABLE",
  origin: "INFERRED",
  transform: "IDENTITY",
  traceability: "KEY_PRESERVING",
  status,
  source_columns: ["email"],
  target_columns: ["body"],
  on_delete_action: null,
  confidence: 0.5,
  evidence: { evidence: "residual_scan", hit_count: 4, note: "value match found by the scan" },
});

beforeEach(() => {
  // React Flow measures its container; jsdom has no ResizeObserver.
  if (!("ResizeObserver" in globalThis)) {
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
  }
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("LineagePage edge review", () => {
  it("shows residual_scan evidence with hit_count and confirms the edge with the token", async () => {
    setCredentials({ token: "secret-token", actor: "reviewer" });
    let status: GraphEdgeOut["status"] = "SUGGESTED";
    const posts: { url: string; body: unknown; headers: Record<string, string> }[] = [];
    const graph = (): SchemaGraphOut => ({
      snapshot_id: SNAP,
      level: "TABLE",
      nodes: [tableNode("customer"), tableNode("document"), tableNode("unrelated")],
      edges: [residualEdge(status)],
    });
    installFetch((url, init) => {
      if (url === "/api/erasure-requests?limit=100") return json(200, []);
      if (url === "/api/lineage/schema?level=TABLE") return json(200, graph());
      if (url === `/api/lineage-edges/${RESIDUAL}/status` && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as { status: GraphEdgeOut["status"] };
        posts.push({ url, body, headers: init.headers as Record<string, string> });
        status = body.status;
        return json(200, residualEdge(status));
      }
      return undefined;
    });
    renderWithProviders(<LineagePage />, { route: "/lineage", path: "/lineage" });

    const queue = await screen.findByRole("table", { name: "Inferred edges" });
    expect(within(queue).getByText(/residual scan:/)).toHaveTextContent("residual scan: 4 hit(s)");
    expect(screen.getByText(/Suggested edges awaiting review \(1 of 1 inferred\)/)).toBeInTheDocument();
    expect(screen.getAllByText(/used by the/).length).toBeGreaterThan(0);

    fireEvent.click(
      within(queue).getByRole("button", { name: "Review edge t:app.customer to t:app.document" }),
    );
    const panel = await screen.findByLabelText("Edge evidence");
    expect(within(panel).getByText(/4 matching row\(s\) after erasure/)).toBeInTheDocument();
    expect(within(panel).getByText(/Residual scan after a LIVE erasure/)).toBeInTheDocument();

    fireEvent.click(
      within(panel).getByRole("button", { name: "Confirm edge t:app.customer to t:app.document" }),
    );
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]?.body).toEqual({ status: "CONFIRMED" });
    expect(posts[0]?.headers["X-LAE-Token"]).toBe("secret-token");
    expect(await within(panel).findByText(/now reports this edge as CONFIRMED/)).toBeInTheDocument();
    // The refetched graph drives the badge and the queue counter.
    await waitFor(() =>
      expect(screen.getByText(/Suggested edges awaiting review \(0 of 1 inferred\)/)).toBeInTheDocument(),
    );
    expect(
      within(panel).getByRole("button", { name: "Confirm edge t:app.customer to t:app.document" }),
    ).toBeDisabled();
  });

  it("shows the server error code when the change is refused", async () => {
    installFetch((url, init) => {
      if (url === "/api/erasure-requests?limit=100") return json(200, []);
      if (url === "/api/lineage/schema?level=TABLE")
        return json(200, {
          snapshot_id: SNAP,
          level: "TABLE",
          nodes: [tableNode("customer"), tableNode("document")],
          edges: [residualEdge("SUGGESTED")],
        });
      if (url.endsWith("/status") && init?.method === "POST")
        return json(401, {
          error: { code: "UNAUTHORIZED", message: "missing or invalid X-LAE-Token", details: {} },
        });
      return undefined;
    });
    renderWithProviders(<LineagePage />, { route: "/lineage", path: "/lineage" });
    fireEvent.click(
      await screen.findByRole("button", { name: "Review edge t:app.customer to t:app.document" }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Reject edge t:app.customer to t:app.document" }),
    );
    expect(await screen.findByText("UNAUTHORIZED")).toBeInTheDocument();
  });
});
