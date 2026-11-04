import { fireEvent, screen } from "@testing-library/react";
import { ErrorState } from "../components/States";
import { OPEN_SETTINGS_EVENT } from "../components/SettingsDialog";
import { ApiError } from "../api/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { installFetch, json, renderWithProviders } from "../test/render";
import { BenchmarksPage } from "./BenchmarksPage";
import { DashboardPage } from "./DashboardPage";
import { VerificationPage } from "./VerificationPage";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("error states", () => {
  it("dashboard shows NETWORK_UNREACHABLE and no numbers when the API is down", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(() => Promise.reject(new TypeError("Failed to fetch"))),
    );
    renderWithProviders(<DashboardPage />);
    const alerts = await screen.findAllByRole("alert");
    expect(alerts.length).toBeGreaterThanOrEqual(3);
    for (const a of alerts) expect(a).toHaveTextContent("NETWORK_UNREACHABLE");
    expect(screen.queryByTestId("stat-synthetic_customers")).toBeNull();
  });

  it("shows the server error code from the envelope", async () => {
    installFetch(() =>
      json(500, { error: { code: "CONFIGURATION", message: "LAE_HMAC_KEY missing", details: {} } }),
    );
    renderWithProviders(<BenchmarksPage />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("CONFIGURATION");
    expect(alert).toHaveTextContent("HTTP 500");
  });
});

describe("auth and four-eyes errors", () => {
  it("a 401 tells the operator to enter the token and opens Settings", async () => {
    installFetch(() =>
      json(401, { error: { code: "UNAUTHORIZED", message: "missing or invalid X-LAE-Token", details: {} } }),
    );
    const opened = vi.fn();
    window.addEventListener(OPEN_SETTINGS_EVENT, opened);
    renderWithProviders(<BenchmarksPage />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("UNAUTHORIZED");
    expect(alert).toHaveTextContent(/Enter a valid API token in Settings/);
    fireEvent.click(screen.getByRole("button", { name: "Open Settings" }));
    expect(opened).toHaveBeenCalledTimes(1);
    window.removeEventListener(OPEN_SETTINGS_EVENT, opened);
  });

  it("explains the separation-of-duties refusal", () => {
    renderWithProviders(
      <ErrorState
        error={new ApiError(409, "INVALID_STATE", "separation of duties: approver may not execute")}
      />,
    );
    expect(screen.getByText(/Four-eyes rule/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open Settings" })).toBeInTheDocument();
  });
});

describe("empty states", () => {
  it("dashboard renders live zero counts and an empty request list", async () => {
    installFetch((url) => {
      if (url === "/api/stats")
        return json(200, {
          synthetic_customers: 0,
          erasure_requests: 0,
          verified_requests: 0,
          partial_requests: 0,
          pending_requests: 0,
          failed_requests: 0,
          discovered_tables: 0,
          discovered_edges: 0,
          registered_artifacts: 0,
          audit_events: 0,
          latest_snapshot_id: null,
          latest_snapshot_at: null,
        });
      if (url === "/api/health")
        return json(200, {
          status: "ok",
          database: true,
          pgvector: "0.8.0",
          pgcrypto: "1.3",
          audit_chain_valid: null,
          signing_key_id: null,
        });
      if (url === "/api/erasure-requests?limit=100") return json(200, []);
      return undefined;
    });
    renderWithProviders(<DashboardPage />);
    expect(await screen.findByText("No erasure requests yet")).toBeInTheDocument();
    expect(await screen.findByTestId("stat-audit_events")).toHaveTextContent("0");
    expect(screen.getByText(/Latest snapshot:\s*none yet/)).toBeInTheDocument();
  });

  it("benchmarks page explains how to record runs when there are none", async () => {
    installFetch(() => json(200, []));
    renderWithProviders(<BenchmarksPage />);
    expect(await screen.findByText("No benchmark runs stored")).toBeInTheDocument();
  });

  it("verification page treats 404 as 'no verification yet' and still shows audit", async () => {
    const REQ = "11111111-1111-1111-1111-111111111111";
    installFetch((url) => {
      if (url === "/api/erasure-requests?limit=100") return json(200, []);
      if (url === `/api/verification/${REQ}`)
        return json(404, { error: { code: "NOT_FOUND", message: "no verification", details: {} } });
      if (url === `/api/audit/${REQ}`)
        return json(200, {
          request_id: REQ,
          events: [],
          chain: {
            valid: true,
            checked_events: 0,
            head_seq: null,
            head_hash: null,
            first_invalid_seq: null,
          },
          evidence: null,
        });
      return undefined;
    });
    renderWithProviders(<VerificationPage />, {
      route: `/verification/${REQ}`,
      path: "/verification/:requestId",
    });
    expect(await screen.findByText("No verification run for this request yet")).toBeInTheDocument();
    expect(await screen.findByText("No evidence has been issued for this request")).toBeInTheDocument();
    expect(screen.getByText("VALID")).toBeInTheDocument();
  });
});

describe("verification summary", () => {
  it("renders outcome, coverage from exact counts, checks and residual hits", async () => {
    const REQ = "11111111-1111-1111-1111-111111111111";
    installFetch((url) => {
      if (url === "/api/erasure-requests?limit=100") return json(200, []);
      if (url === `/api/verification/${REQ}`)
        return json(200, {
          id: "88888888-8888-8888-8888-888888888888",
          execution_run_id: "99999999-9999-9999-9999-999999999999",
          status: "PASSED",
          outcome: "PARTIAL",
          required_artifacts: 3,
          verified_artifacts: 2,
          coverage: 0.6667,
          checks: [
            {
              check_type: "ROW_ABSENT",
              schema_name: "app",
              table_name: "order",
              column_name: null,
              step_order: 1,
              passed: true,
              observed: { rows: 0 },
              expected: { rows: 0 },
            },
          ],
          residual_hits: [{ table: "app.document", column: "body", count: 1 }],
          started_at: "2026-11-04T10:00:00Z",
          finished_at: "2026-11-04T10:00:01Z",
          duration_ms: 1000,
        });
      if (url === `/api/audit/${REQ}`)
        return json(500, { error: { code: "AUDIT_INTEGRITY", message: "chain broken", details: {} } });
      return undefined;
    });
    renderWithProviders(<VerificationPage />, {
      route: `/verification/${REQ}`,
      path: "/verification/:requestId",
    });
    expect(await screen.findByText("PARTIAL")).toBeInTheDocument();
    expect(screen.getByTestId("coverage")).toHaveTextContent("2/3 (66.6%)");
    expect(screen.getByText("ROW_ABSENT")).toBeInTheDocument();
    expect(screen.getByText("app.document")).toBeInTheDocument();
    expect(await screen.findByText("AUDIT_INTEGRITY")).toBeInTheDocument();
  });
});
