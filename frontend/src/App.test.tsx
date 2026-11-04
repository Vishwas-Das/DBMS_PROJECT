import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppRoutes, makeQueryClient } from "./App";
import { getCredentials } from "./api/credentials";

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderApp(route: string) {
  const client = makeQueryClient();
  client.setDefaultOptions({ queries: { retry: false } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <AppRoutes />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("App shell", () => {
  it("shows the nav, an API-unavailable header and the page error when the backend is down", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(() => Promise.reject(new TypeError("Failed to fetch"))),
    );
    renderApp("/");
    const nav = screen.getByRole("navigation", { name: "Main" });
    for (const label of [
      "Dashboard",
      "Erasure Requests",
      "Lineage Explorer",
      "Erasure Plan",
      "Execution Monitor",
      "Verification & Audit",
      "Benchmarks",
    ]) {
      expect(nav).toHaveTextContent(label);
    }
    expect(await screen.findByText(/API unavailable \(NETWORK_UNREACHABLE\)/)).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Dashboard" })).toBeInTheDocument();
    expect(screen.getByText("No token: enter it in Settings")).toBeInTheDocument();
  });

  it("stores the operator token from the Settings dialog in sessionStorage only", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(() => Promise.reject(new TypeError("Failed to fetch"))),
    );
    renderApp("/benchmarks");
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    fireEvent.change(screen.getByLabelText("API token (LAE_API_TOKEN)"), {
      target: { value: "tok" },
    });
    fireEvent.change(screen.getByLabelText(/Approver name/), { target: { value: "dana" } });
    fireEvent.change(screen.getByLabelText(/Executor name/), { target: { value: "eve" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(getCredentials()).toEqual({ token: "tok", actor: "dana", executor: "eve" });
    expect(window.sessionStorage.getItem("lae.apiToken")).toBe("tok");
    expect(window.localStorage.length).toBe(0);
    expect(await screen.findByText(/Token set, approver dana, executor eve/)).toBeInTheDocument();
    expect(window.sessionStorage.getItem("lae.executor")).toBe("eve");
  });
});
