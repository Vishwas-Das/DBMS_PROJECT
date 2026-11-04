import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { installFetch, json, renderWithProviders } from "../test/render";
import { ExecutionPage } from "./ExecutionPage";

const REQ = "11111111-1111-1111-1111-111111111111";
const PLAN = "22222222-2222-2222-2222-222222222222";

afterEach(() => {
  vi.unstubAllGlobals();
});

function setup() {
  const executeBodies: unknown[] = [];
  const f = installFetch((url, init) => {
    if (url === "/api/erasure-requests?limit=100") return json(200, []);
    if (url === `/api/erasure-requests/${REQ}`)
      return json(200, {
        id: REQ,
        subject_root: "customer",
        subject_key_hmac: "c".repeat(64),
        status: "APPROVED",
        requested_by: "op",
        created_at: "2026-11-04T09:00:00Z",
        completed_at: null,
        current_plan_id: PLAN,
        latest_outcome: null,
      });
    if (url === `/api/erasure-plans/${PLAN}` && (init?.method ?? "GET") === "GET")
      return json(200, {
        id: PLAN,
        request_id: REQ,
        version: 1,
        status: "APPROVED",
        snapshot_id: "33333333-3333-3333-3333-333333333333",
        schema_fingerprint: "a".repeat(64),
        record_set_hash: "b".repeat(64),
        plan_hash: "d".repeat(64),
        record_count: 2,
        items: [],
        conflicts: [],
        counts_by_category: {},
        is_blocked: false,
        created_at: "2026-11-04T10:00:00Z",
        approved_by: "op",
        approved_at: "2026-11-04T10:01:00Z",
      });
    if (url === `/api/erasure-plans/${PLAN}/execute` && init?.method === "POST") {
      const body = JSON.parse(String(init.body)) as { mode: string };
      executeBodies.push(body);
      return json(200, {
        id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        plan_id: PLAN,
        mode: body.mode,
        status: body.mode === "LIVE" ? "COMMITTED" : "ROLLED_BACK",
        started_at: "2026-11-04T10:02:00Z",
        finished_at: "2026-11-04T10:02:01Z",
        steps: [
          { step_order: 1, action: "DELETE_ROWS", category: "DELETE", table: "app.order", status: "APPLIED", rows_affected: 2, expected_rows: 2, duration_ms: 3 },
          { step_order: 2, action: "UNRESOLVED", category: "UNRESOLVED", table: "app.ext", status: "SKIPPED", rows_affected: null, expected_rows: null, duration_ms: 0 },
          { step_order: 3, action: "SCHEDULE_EXTERNAL", category: "SCHEDULE", table: "app.crm", status: "SKIPPED", rows_affected: null, expected_rows: null, duration_ms: 0 },
        ],
        error: null,
        verification: null,
        evidence_id: null,
        timings_ms: {},
      });
    }
    return undefined;
  });
  renderWithProviders(<ExecutionPage />, { route: `/execution/${REQ}`, path: "/execution/:requestId" });
  return { f, executeBodies };
}

describe("ExecutionPage", () => {
  it("runs a dry run directly and summarises steps", async () => {
    const { executeBodies } = setup();
    fireEvent.click(await screen.findByRole("button", { name: "Dry run" }));
    await waitFor(() => expect(executeBodies).toEqual([{ mode: "DRY_RUN" }]));
    const summary = await screen.findByLabelText("Step summary");
    expect(within(summary).getByText("Skipped").nextSibling).toHaveTextContent("2");
    expect(summary).toHaveTextContent("unresolved 1, scheduled 1, manual 0");
    expect(screen.getByText("ROLLED_BACK")).toBeInTheDocument();
  });

  it("requires an explicit confirmation before LIVE execution", async () => {
    const { executeBodies } = setup();
    fireEvent.click(await screen.findByRole("button", { name: "Execute LIVE..." }));
    const dialog = screen.getByRole("dialog", { name: "Confirm LIVE execution" });
    const go = within(dialog).getByRole("button", { name: "Execute LIVE now" });
    expect(go).toBeDisabled();
    expect(executeBodies).toEqual([]);

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(executeBodies).toEqual([]);

    fireEvent.click(screen.getByRole("button", { name: "Execute LIVE..." }));
    const d2 = screen.getByRole("dialog", { name: "Confirm LIVE execution" });
    fireEvent.click(within(d2).getByRole("checkbox"));
    fireEvent.click(within(d2).getByRole("button", { name: "Execute LIVE now" }));
    await waitFor(() => expect(executeBodies).toEqual([{ mode: "LIVE" }]));
    expect(await screen.findByText("COMMITTED")).toBeInTheDocument();
  });
});
