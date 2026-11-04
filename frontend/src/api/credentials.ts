/**
 * Operator credentials for mutating endpoints (CR lead-1 §4: X-LAE-Token / X-LAE-Actor).
 * Kept in sessionStorage ONLY: never localStorage, never hard-coded, never logged.
 */
const TOKEN_KEY = "lae.apiToken";
const ACTOR_KEY = "lae.actor";

export interface Credentials {
  token: string | null;
  actor: string | null;
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
  };
  return cached;
}

export function setCredentials(next: Credentials): void {
  const s = storage();
  const token = next.token?.trim() || null;
  const actor = next.actor?.trim() || null;
  if (s) {
    if (token) s.setItem(TOKEN_KEY, token);
    else s.removeItem(TOKEN_KEY);
    if (actor) s.setItem(ACTOR_KEY, actor);
    else s.removeItem(ACTOR_KEY);
  }
  cached = { token, actor };
  listeners.forEach((l) => l());
}

export function clearCredentials(): void {
  setCredentials({ token: null, actor: null });
}

export function subscribeCredentials(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
