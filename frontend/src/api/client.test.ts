import { describe, expect, it, vi } from "vitest";
import {
  ApiError,
  CLIENT_ERROR_CODES,
  actorFor,
  buildUrl,
  createClient,
  parseErrorPayload,
} from "./client";
import { getCredentials, setCredentials } from "./credentials";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mockFetch(res: Response | (() => Promise<Response>)) {
  return vi.fn<typeof fetch>(() => (typeof res === "function" ? res() : Promise.resolve(res)));
}

function headersOf(f: ReturnType<typeof mockFetch>, call = 0): Record<string, string> {
  const init = f.mock.calls[call]?.[1];
  return (init?.headers ?? {}) as Record<string, string>;
}

describe("parseErrorPayload", () => {
  it("parses the error envelope", () => {
    const e = parseErrorPayload(409, {
      error: { code: "STALE_PLAN", message: "schema drifted", details: { table: "app.order" } },
    });
    expect(e).toBeInstanceOf(ApiError);
    expect(e.status).toBe(409);
    expect(e.code).toBe("STALE_PLAN");
    expect(e.message).toBe("schema drifted");
    expect(e.details).toEqual({ table: "app.order" });
  });

  it("falls back to HTTP_<status> when the body is not an envelope", () => {
    const e = parseErrorPayload(502, { detail: "bad gateway" }, "Bad Gateway");
    expect(e.code).toBe("HTTP_502");
    expect(e.message).toBe("Bad Gateway");
    expect(parseErrorPayload(500, undefined).code).toBe("HTTP_500");
  });

  it("tolerates missing message and non-object details", () => {
    const e = parseErrorPayload(401, { error: { code: "UNAUTHORIZED", details: [1] } });
    expect(e.code).toBe("UNAUTHORIZED");
    expect(e.message).toBe("");
    expect(e.details).toEqual({});
  });
});

describe("buildUrl", () => {
  it("fills and encodes path params and drops empty query values", () => {
    expect(
      buildUrl("", "/api/lineage/{request_id}", { request_id: "a b" }, { level: "COLUMN", x: undefined, y: "" }),
    ).toBe("/api/lineage/a%20b?level=COLUMN");
  });
  it("refuses to send a request with a missing path param", () => {
    expect(() => buildUrl("", "/api/erasure-plans/{plan_id}", {})).toThrow(ApiError);
  });
});

describe("client requests", () => {
  it("returns parsed JSON on success and sends the token on GET too", async () => {
    setCredentials({ token: "t0k3n", actor: "alice" });
    const f = mockFetch(jsonResponse(200, { status: "ok" }));
    const c = createClient({ fetchImpl: f });
    await expect(c.get("/api/health")).resolves.toEqual({ status: "ok" });
    expect(f.mock.calls[0]?.[0]).toBe("/api/health");
    expect(f.mock.calls[0]?.[1]?.method).toBe("GET");
    expect(headersOf(f)["X-LAE-Token"]).toBe("t0k3n");
    expect(headersOf(f)["X-LAE-Actor"]).toBe("alice");
    expect(headersOf(f)["Content-Type"]).toBeUndefined();
  });

  it("sends the executor name as X-LAE-Actor for execute calls (four-eyes)", async () => {
    setCredentials({ token: "t0k3n", actor: "alice", executor: "bob" });
    const f = mockFetch(() => Promise.resolve(jsonResponse(200, {})));
    const c = createClient({ fetchImpl: f });
    await c.post("/api/erasure-plans/{plan_id}/execute", {
      path: { plan_id: "p1" },
      body: { mode: "LIVE" },
      as: "executor",
    });
    expect(headersOf(f)["X-LAE-Actor"]).toBe("bob");
    await c.post("/api/erasure-plans/{plan_id}/approve", {
      path: { plan_id: "p1" },
      body: { plan_hash: "h" },
    });
    expect(headersOf(f, 1)["X-LAE-Actor"]).toBe("alice");
  });

  it("falls back to the default actor when no executor name is set", () => {
    expect(actorFor({ token: null, actor: "alice", executor: null }, "executor")).toBe("alice");
    expect(actorFor({ token: null, actor: null, executor: "bob" })).toBeNull();
  });

  it("injects X-LAE-Token and X-LAE-Actor on mutations", async () => {
    setCredentials({ token: "t0k3n", actor: "alice" });
    const f = mockFetch(jsonResponse(200, { valid: true }));
    const c = createClient({ fetchImpl: f });
    await c.post("/api/audit/verify-chain");
    const h = headersOf(f);
    expect(h["X-LAE-Token"]).toBe("t0k3n");
    expect(h["X-LAE-Actor"]).toBe("alice");
    expect(h["Content-Type"]).toBe("application/json");
    expect(f.mock.calls[0]?.[1]?.method).toBe("POST");
  });

  it("omits auth headers when no token is set, so the server answers 401", async () => {
    const f = mockFetch(
      jsonResponse(401, { error: { code: "UNAUTHORIZED", message: "missing or invalid X-LAE-Token", details: {} } }),
    );
    const c = createClient({ fetchImpl: f });
    const err = await c.post("/api/discovery/{request_id}", { path: { request_id: "r1" } }).catch((e: unknown) => e);
    expect(headersOf(f)["X-LAE-Token"]).toBeUndefined();
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe("UNAUTHORIZED");
    expect((err as ApiError).status).toBe(401);
  });

  it("serialises the request body", async () => {
    const f = mockFetch(jsonResponse(201, { id: "r1" }));
    const c = createClient({ fetchImpl: f });
    await c.post("/api/erasure-requests", { body: { subject_root: "customer", subject_key: [7] } });
    expect(JSON.parse(String(f.mock.calls[0]?.[1]?.body))).toEqual({ subject_root: "customer", subject_key: [7] });
  });

  it("turns error envelopes into ApiError", async () => {
    const f = mockFetch(
      jsonResponse(409, { error: { code: "PLAN_HASH_MISMATCH", message: "hash differs", details: {} } }),
    );
    const c = createClient({ fetchImpl: f });
    await expect(c.get("/api/erasure-plans/{plan_id}", { path: { plan_id: "p" } })).rejects.toMatchObject({
      code: "PLAN_HASH_MISMATCH",
      status: 409,
    });
  });

  it("maps network failures to NETWORK_UNREACHABLE", async () => {
    const f = mockFetch(() => Promise.reject(new TypeError("Failed to fetch")));
    const c = createClient({ fetchImpl: f });
    await expect(c.get("/api/stats")).rejects.toMatchObject({ code: CLIENT_ERROR_CODES.network, status: 0 });
  });

  it("reports non-JSON success bodies and non-JSON error pages", async () => {
    const ok = createClient({ fetchImpl: mockFetch(new Response("<html>", { status: 200 })) });
    await expect(ok.get("/api/stats")).rejects.toMatchObject({ code: CLIENT_ERROR_CODES.badResponse });
    const bad = createClient({
      fetchImpl: mockFetch(new Response("<html>proxy error</html>", { status: 504, statusText: "Gateway Timeout" })),
    });
    await expect(bad.get("/api/stats")).rejects.toMatchObject({ code: "HTTP_504", status: 504 });
  });

  it("downloads blobs and still parses error envelopes for them", async () => {
    const ok = createClient({ fetchImpl: mockFetch(new Response('{"bundle":1}', { status: 200 })) });
    const blob = await ok.getBlob("/api/evidence/{request_id}/download", { path: { request_id: "r" } });
    expect(await blob.text()).toBe('{"bundle":1}');
    const nf = createClient({
      fetchImpl: mockFetch(jsonResponse(404, { error: { code: "NOT_FOUND", message: "no evidence", details: {} } })),
    });
    await expect(
      nf.getBlob("/api/evidence/{request_id}/leaves", { path: { request_id: "r" } }),
    ).rejects.toMatchObject({ code: "NOT_FOUND" });
  });
});

describe("credentials", () => {
  it("are kept in sessionStorage only", () => {
    setCredentials({ token: " abc ", actor: "" });
    expect(getCredentials()).toEqual({ token: "abc", actor: null, executor: null });
    expect(window.sessionStorage.getItem("lae.apiToken")).toBe("abc");
    expect(window.localStorage.getItem("lae.apiToken")).toBeNull();
  });
});
