/**
 * Operator credentials (X-LAE-Token / X-LAE-Actor). Every API call except /api/health needs the
 * token. Kept in sessionStorage ONLY: never localStorage, never hard-coded, never logged.
 *
 * Four-eyes rule: the server rejects a LIVE execution whose X-LAE-Actor equals the plan's
 * approver. `actor` is the approver / default name; `executor` is sent for execute calls.
 */
const TOKEN_KEY = "lae.apiToken";
const ACTOR_KEY = "lae.actor";
const EXECUTOR_KEY = "lae.executor";

export interface Credentials {
  token: string | null;
  /** Approver name, also the default X-LAE-Actor for every other call. */
  actor: string | null;
  /** X-LAE-Actor for execute calls (dry run and LIVE). Falls back to `actor` when empty. */
  executor: string | null;
}

export interface CredentialsInput {
  token: string | null;
  actor: string | null;
  executor?: string | null;
}

type Listener = () => void;
const listeners = new Set<Listener>();

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

let cached: Credentials | null = null;

export function getCredentials(): Credentials {
  if (cached) return cached;
  const s = storage();
  cached = {
    token: s?.getItem(TOKEN_KEY) || null,
    actor: s?.getItem(ACTOR_KEY) || null,
    executor: s?.getItem(EXECUTOR_KEY) || null,
  };
  return cached;
}

function put(s: Storage, key: string, v: string | null): void {
  if (v) s.setItem(key, v);
  else s.removeItem(key);
}

export function setCredentials(next: CredentialsInput): void {
  const s = storage();
  const token = next.token?.trim() || null;
  const actor = next.actor?.trim() || null;
  const executor = next.executor?.trim() || null;
  if (s) {
    put(s, TOKEN_KEY, token);
    put(s, ACTOR_KEY, actor);
    put(s, EXECUTOR_KEY, executor);
  }
  cached = { token, actor, executor };
  listeners.forEach((l) => l());
}

export function clearCredentials(): void {
  setCredentials({ token: null, actor: null, executor: null });
}

export function subscribeCredentials(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
