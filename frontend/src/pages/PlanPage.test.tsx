import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setCredentials } from "../api/credentials";
import type { PlanOut } from "../api/types";
import { installFetch, json, renderWithProviders } from "../test/render";
import { ApprovePanel, PlanPage } from "./PlanPage";

// Test-only fixture. An arbitrary 64-hex value, deliberately not derivable from the plan.
const HASH = "0f1e2d3c4b5a69788796a5b4c3d2e1f00112233445566778899aabbccddeeff0";
const REQ = "11111111-1111-1111-1111-111111111111";
const PLAN = "22222222-2222-2222-2222-222222222222";

const plan = (over: Partial<PlanOut> = {}): PlanOut => ({
  id: PLAN,
  request_id: REQ,
  version: 1,
  status: "DRAFT",
  snapshot_id: "33333333-3333-3333-3333-333333333333",
  schema_fingerprint: "a".repeat(64),
  record_set_hash: "b".repeat(64),
  plan_hash: HASH,
  record_count: 3,
  items: [
    {
      id: "44444444-4444-4444-4444-444444444444",
      step_order: 1,
      action: "DELETE_ROWS",
      category: "DELETE",
      schema_name: "app",
      table_name: "order",
      lineage_level: "RECORD",
      key_columns: ["id"],
      record_count: 3,
      record_key_hmacs: [],
      target_columns: [],
      expected_row_count: 3,
      depends_on_steps: [],
      reason_edge_ids: ["55555555-5555-5555-5555-555555555555"],
      reason: "owned by subject via FK",
      retention_policy: null,
    },
  ],
  conflicts: [
    {
      id: "66666666-6666-6666-6666-666666666666",
      code: "OPAQUE_ARTIFACT",
      severity: "BLOCKING",
      schema_name: "app",
      table_name: "weird_view",
      message: "opaque SQL",
      resolution: null,
      acknowledgeable: true,
    },
    {
      id: "77777777-7777-7777-7777-777777777777",
      code: "UNBREAKABLE_CYCLE",
      severity: "BLOCKING",
      schema_name: "app",
      table_name: "cyc",
      message: "cycle",
      resolution: null,
      acknowledgeable: false,
    },
  ],
  counts_by_category: { DELETE: 1 },
  is_blocked: false,
  created_at: "2026-11-04T10:00:00Z",
  approved_by: null,
  approved_at: null,
  ...over,
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ApprovePanel", () => {
  it("shows the full plan hash and sends it back exactly as received, with auth headers", async () => {
    setCredentials({ token: "secret-token", actor: "reviewer" });
    const f = installFetch((url, init) =>
      url === `/api/erasure-plans/${PLAN}/approve` && init?.method === "POST"
        ? json(200, plan({ status: "APPROVED", approved_by: "reviewer", approved_at: "2026-11-04T10:05:00Z" }))
        : undefined,
    );
    renderWithProviders(<ApprovePanel plan={plan()} requestId={REQ} />);

    expect(screen.getByTestId("hash-plan_hash").textContent).toBe(HASH);
    fireEvent.click(screen.getByRole("button", { name: "Approve this plan hash" }));

    await waitFor(() => expect(f).toHaveBeenCalled());
    const init = f.mock.calls[0]?.[1];
    expect(JSON.parse(String(init?.body))).toEqual({ plan_hash: HASH });
    const headers = init?.headers as Record<string, string>;
    expect(headers["X-LAE-Token"]).toBe("secret-token");
    expect(headers["X-LAE-Actor"]).toBe("reviewer");
    expect(await screen.findByText(/Plan approved \(status APPROVED\)/)).toBeInTheDocument();
  });

  it("is disabled when the server reports the plan as blocked", () => {
    installFetch(() => undefined);
    renderWithProviders(<ApprovePanel plan={plan({ is_blocked: true })} requestId={REQ} />);
    expect(screen.getByRole("button", { name: "Approve this plan hash" })).toBeDisabled();
  });

  it("shows the error code and offers Re-plan on STALE_PLAN", async () => {
    installFetch(() =>
      json(409, { error: { code: "STALE_PLAN", message: "schema fingerprint changed", details: {} } }),
    );
    const onReplan = vi.fn();
    renderWithProviders(<ApprovePanel plan={plan()} requestId={REQ} onReplan={onReplan} />);
    fireEvent.click(screen.getByRole("button", { name: "Approve this plan hash" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("STALE_PLAN");
    expect(alert).toHaveTextContent("schema fingerprint changed");
    fireEvent.click(screen.getByRole("button", { name: "Re-plan" }));
    expect(onReplan).toHaveBeenCalledOnce();
  });

  it("does not offer Re-plan for other errors", async () => {
    installFetch(() =>
      json(409, { error: { code: "PLAN_HASH_MISMATCH", message: "hash differs", details: {} } }),
    );
    renderWithProviders(<ApprovePanel plan={plan()} requestId={REQ} onReplan={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Approve this plan hash" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("PLAN_HASH_MISMATCH");
    expect(screen.queryByRole("button", { name: "Re-plan" })).toBeNull();
  });
});

describe("PlanPage", () => {
  it("renders items, edge relationships and only acknowledgeable conflict buttons", async () => {
    installFetch((url) => {
      if (url === "/api/erasure-requests?limit=100") return json(200, []);
      if (url === `/api/erasure-requests/${REQ}`)
        return json(200, {
          id: REQ,
          subject_root: "customer",
          subject_key_hmac: "c".repeat(64),
          status: "PLANNED",
          requested_by: "op",
          created_at: "2026-11-04T09:00:00Z",
          completed_at: null,
          current_plan_id: PLAN,
          latest_outcome: null,
        });
      if (url === `/api/erasure-plans/${PLAN}`) return json(200, plan());
      if (url.startsWith(`/api/lineage/${REQ}`))
        return json(200, {
          request_id: REQ,
          plan_id: PLAN,
          snapshot_id: "33333333-3333-3333-3333-333333333333",
          schema_graph: {
            snapshot_id: "33333333-3333-3333-3333-333333333333",
            level: "TABLE",
            nodes: [],
            edges: [
              {
                id: "55555555-5555-5555-5555-555555555555",
                source: "app.customer",
                target: "app.order",
                level: "TABLE",
                origin: "DECLARED_FK",
                transform: "FOREIGN_KEY",
                traceability: "KEY_PRESERVING",
                status: "CONFIRMED",
                source_columns: ["id"],
                target_columns: ["customer_id"],
                evidence: { constraint: "fk_order_customer" },
              },
            ],
          },
          records: [],
          record_edges: [],
        });
      return undefined;
    });
    renderWithProviders(<PlanPage />, { route: `/plan/${REQ}`, path: "/plan/:requestId" });

    expect(await screen.findByText("owned by subject via FK")).toBeInTheDocument();
    const edgeBtn = await screen.findByRole("button", {
      name: "Show evidence for edge app.customer to app.order",
    });
    fireEvent.click(edgeBtn);
    expect(await screen.findByText(/fk_order_customer/)).toBeInTheDocument();

    expect(screen.getAllByRole("button", { name: "Acknowledge" })).toHaveLength(1);
    expect(screen.getByText("not acknowledgeable")).toBeInTheDocument();
  });
});
