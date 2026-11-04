/**
 * Typed fetch wrapper for the LAE API (docs/openapi.json).
 * - Request and response types come from the generated `paths` type, so a contract change breaks the build.
 * - Non-2xx responses are parsed from the `{"error": {code, message, details}}` envelope into ApiError.
 * - Every request carries X-LAE-Token / X-LAE-Actor from the sessionStorage credentials
 *   (all endpoints except /api/health require the token, GETs included).
 */
import type { paths } from "./schema";
import { getCredentials, type Credentials } from "./credentials";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(
    status: number,
    code: string,
    message: string,
    details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

/** Client-side codes, used only when the server could not produce an envelope at all. */
export const CLIENT_ERROR_CODES = {
  network: "NETWORK_UNREACHABLE",
  badResponse: "BAD_RESPONSE",
  missingParam: "CLIENT_MISSING_PARAM",
} as const;

type Method = "get" | "post";
type PathsWith<M extends Method> = {
  [P in keyof paths]: paths[P][M] extends undefined ? never : P;
}[keyof paths];
type Op<P extends keyof paths, M extends Method> = NonNullable<paths[P][M]>;

type JsonContent<R> = R extends { content: { "application/json": infer B } } ? B : never;
type Responses<P extends keyof paths, M extends Method> =
  Op<P, M> extends { responses: infer R } ? R : never;

export type OkBody<P extends keyof paths, M extends Method> =
  Responses<P, M> extends { 200: infer R }
    ? JsonContent<R>
    : Responses<P, M> extends { 201: infer R }
      ? JsonContent<R>
      : never;

export type BodyOf<P extends keyof paths, M extends Method> =
  Op<P, M> extends { requestBody: { content: { "application/json": infer B } } }
    ? B
    : Op<P, M> extends { requestBody?: { content: { "application/json": infer B } } }
      ? B | undefined
      : undefined;

type ParamsOf<P extends keyof paths, M extends Method, K extends "path" | "query"> =
  Op<P, M> extends { parameters: { [Q in K]?: infer X } } ? Exclude<X, undefined> : never;

export interface RequestOptions<P extends keyof paths, M extends Method> {
  path?: ParamsOf<P, M, "path">;
  query?: ParamsOf<P, M, "query">;
  body?: BodyOf<P, M>;
  signal?: AbortSignal;
  /** Which configured actor name to send: the executor name for execute calls (four-eyes). */
  as?: ActorRole;
}

export type ActorRole = "default" | "executor";

interface RawOptions {
  path?: Record<string, unknown>;
  query?: Record<string, unknown>;
  body?: unknown;
  signal?: AbortSignal;
  as?: ActorRole;
}

/** The X-LAE-Actor value for a role. The executor falls back to the default actor. */
export function actorFor(c: Credentials, role: ActorRole = "default"): string | null {
  return role === "executor" ? (c.executor ?? c.actor) : c.actor;
}

export interface ClientConfig {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
  credentials?: () => Credentials;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Parse an ErrorEnvelope. Anything that is not an envelope becomes `HTTP_<status>`. */
export function parseErrorPayload(status: number, payload: unknown, statusText = ""): ApiError {
  if (isRecord(payload) && isRecord(payload.error)) {
    const e = payload.error;
    if (typeof e.code === "string") {
      return new ApiError(
        status,
        e.code,
        typeof e.message === "string" ? e.message : "",
        isRecord(e.details) ? e.details : {},
      );
    }
  }
  return new ApiError(
    status,
    `HTTP_${status}`,
    statusText || "unexpected response without an error envelope",
  );
}

export function buildUrl(
  baseUrl: string,
  template: string,
  pathParams?: Record<string, unknown>,
  query?: Record<string, unknown>,
): string {
  const path = template.replace(/\{([a-z_]+)\}/g, (_m, name: string) => {
    const v = pathParams?.[name];
    if (v === undefined || v === null || v === "") {
      throw new ApiError(0, CLIENT_ERROR_CODES.missingParam, `missing path parameter ${name}`);
    }
    return encodeURIComponent(String(v));
  });
  const qs = new URLSearchParams();
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
    }
  }
  const s = qs.toString();
  return `${baseUrl}${path}${s ? `?${s}` : ""}`;
}

export function createClient(config: ClientConfig = {}) {
  const baseUrl = config.baseUrl ?? "";
  const doFetch: typeof fetch =
    config.fetchImpl ?? ((input, init) => globalThis.fetch(input, init));
  const creds = config.credentials ?? getCredentials;

  async function send(method: Method, template: string, o: RawOptions = {}): Promise<Response> {
    const url = buildUrl(baseUrl, template, o.path, o.query);
    const headers: Record<string, string> = { Accept: "application/json" };
    if (method === "post") headers["Content-Type"] = "application/json";
    const c = creds();
    if (c.token) headers["X-LAE-Token"] = c.token;
    const actor = actorFor(c, o.as);
    if (actor) headers["X-LAE-Actor"] = actor;
    const init: RequestInit = { method: method.toUpperCase(), headers };
    if (method === "post" && o.body !== undefined) init.body = JSON.stringify(o.body);
    if (o.signal) init.signal = o.signal;
    try {
      return await doFetch(url, init);
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") throw err;
      throw new ApiError(
        0,
        CLIENT_ERROR_CODES.network,
        "API unreachable (network error or server not running)",
      );
    }
  }

  async function readJson<T>(res: Response): Promise<T> {
    const text = await res.text();
    let payload: unknown = undefined;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        if (!res.ok) {
          throw new ApiError(res.status, `HTTP_${res.status}`, res.statusText || "non-JSON error");
        }
        throw new ApiError(res.status, CLIENT_ERROR_CODES.badResponse, "response is not valid JSON");
      }
    }
    if (!res.ok) throw parseErrorPayload(res.status, payload, res.statusText);
    return payload as T;
  }

  return {
    async get<P extends PathsWith<"get">>(
      template: P,
      opts?: RequestOptions<P, "get">,
    ): Promise<OkBody<P, "get">> {
      return readJson(await send("get", template, opts as RawOptions | undefined));
    },
    async post<P extends PathsWith<"post">>(
      template: P,
      opts?: RequestOptions<P, "post">,
    ): Promise<OkBody<P, "post">> {
      return readJson(await send("post", template, opts as RawOptions | undefined));
    },
    /** Attachment endpoints (evidence download / leaves): returns the raw bytes unchanged. */
    async getBlob<P extends PathsWith<"get">>(
      template: P,
      opts?: RequestOptions<P, "get">,
    ): Promise<Blob> {
      const res = await send("get", template, opts as RawOptions | undefined);
      if (!res.ok) await readJson<never>(res);
      return res.blob();
    },
  };
}

export type ApiClient = ReturnType<typeof createClient>;

export const api: ApiClient = createClient();

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError;
}
