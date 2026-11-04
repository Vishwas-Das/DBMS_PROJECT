import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { vi } from "vitest";

/** Test-only helpers. fetch is mocked inside tests, never in the shipped app. */
export function renderWithProviders(
  ui: ReactElement,
  { route = "/", path = "*" }: { route?: string; path?: string } = {},
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } },
  });
  const result = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path={path} element={ui} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...result, client };
}

export function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export type Route_ = (url: string, init: RequestInit | undefined) => Response | undefined;

/** Installs a fetch mock that routes by "METHOD path" and fails loudly on anything unexpected. */
export function installFetch(handler: Route_) {
  const f = vi.fn<typeof fetch>((input, init) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const res = handler(url, init);
    if (!res) return Promise.reject(new Error(`unexpected fetch ${init?.method ?? "GET"} ${url}`));
    return Promise.resolve(res);
  });
  vi.stubGlobal("fetch", f);
  return f;
}
