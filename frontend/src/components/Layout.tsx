import { Suspense, useEffect, useState, useSyncExternalStore } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { NavLink, Outlet } from "react-router-dom";
import { useHealth } from "../api/hooks";
import { getCredentials, subscribeCredentials } from "../api/credentials";
import { ApiError } from "../api/client";
import { Pill } from "./Badges";
import { OPEN_SETTINGS_EVENT, SettingsDialog } from "./SettingsDialog";
import { Loading } from "./States";

const NAV = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/requests", label: "Erasure Requests" },
  { to: "/lineage", label: "Lineage Explorer" },
  { to: "/plan", label: "Erasure Plan" },
  { to: "/execution", label: "Execution Monitor" },
  { to: "/verification", label: "Verification & Audit" },
  { to: "/benchmarks", label: "Benchmarks" },
];

function HealthIndicator() {
  const h = useHealth();
  if (h.error) {
    const code = h.error instanceof ApiError ? h.error.code : "ERROR";
    return (
      <Pill tone="red" symbol={"✗"} title="API health check failed">
        API unavailable ({code})
      </Pill>
    );
  }
  if (!h.data) return <Pill tone="slate">API: checking...</Pill>;
  const d = h.data;
  return (
    <div className="flex flex-wrap items-center gap-2" aria-label="API health">
      <Pill tone={d.status === "ok" ? "green" : "amber"} symbol={d.status === "ok" ? "✓" : "!"}>
        API {d.status}
      </Pill>
      <Pill tone={d.database ? "green" : "red"} symbol={d.database ? "✓" : "✗"}>
        DB {d.database ? "up" : "down"}
      </Pill>
      <Pill
        tone={d.audit_chain_valid === null ? "slate" : d.audit_chain_valid ? "green" : "red"}
        symbol={d.audit_chain_valid === null ? "-" : d.audit_chain_valid ? "✓" : "✗"}
      >
        Audit chain{" "}
        {d.audit_chain_valid === null ? "unknown" : d.audit_chain_valid ? "valid" : "INVALID"}
      </Pill>
    </div>
  );
}

function TokenStatus() {
  const creds = useSyncExternalStore(subscribeCredentials, getCredentials);
  return creds.token ? (
    <Pill tone="green" symbol={"✓"}>
      Token set{creds.actor ? `, approver ${creds.actor}` : ""}
      {creds.executor ? `, executor ${creds.executor}` : ""}
    </Pill>
  ) : (
    <Pill tone="amber" symbol="!">
      No token: enter it in Settings
    </Pill>
  );
}

export function Layout() {
  const [settingsOpen, setSettingsOpen] = useState(false);
  const qc = useQueryClient();
  useEffect(() => {
    const open = () => setSettingsOpen(true);
    window.addEventListener(OPEN_SETTINGS_EVENT, open);
    // New credentials: refetch everything (queries that failed with 401 recover).
    const unsubscribe = subscribeCredentials(() => void qc.invalidateQueries());
    return () => {
      window.removeEventListener(OPEN_SETTINGS_EVENT, open);
      unsubscribe();
    };
  }, [qc]);
  return (
    <div className="flex min-h-screen">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:p-2">
        Skip to content
      </a>
      <nav aria-label="Main" className="w-56 shrink-0 border-r border-slate-200 bg-slate-900 text-slate-100">
        <div className="px-4 py-4">
          <p className="text-lg font-bold">LAE</p>
          <p className="text-xs text-slate-400">Lineage-Aware Erasure Engine</p>
        </div>
        <ul className="space-y-0.5 px-2">
          {NAV.map((n) => (
            <li key={n.to}>
              <NavLink
                to={n.to}
                end={n.end}
                className={({ isActive }) =>
                  `block rounded px-3 py-2 text-sm ${
                    isActive ? "bg-slate-700 font-semibold" : "hover:bg-slate-800"
                  }`
                }
              >
                {({ isActive }) => (
                  <>
                    {isActive ? <span aria-hidden="true">&#9656; </span> : null}
                    {n.label}
                  </>
                )}
              </NavLink>
            </li>
          ))}
        </ul>
        <p className="px-4 pt-6 text-[11px] leading-snug text-slate-400">
          Research prototype. Shows only what the API returns.
        </p>
      </nav>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 bg-white px-6 py-3">
          <HealthIndicator />
          <div className="flex items-center gap-2">
            <TokenStatus />
            <button type="button" className="btn-secondary" onClick={() => setSettingsOpen(true)}>
              Settings
            </button>
          </div>
        </header>
        <main id="main" className="min-w-0 flex-1 space-y-4 p-6">
          <Suspense fallback={<Loading label="Loading page" />}>
            <Outlet />
          </Suspense>
        </main>
      </div>
      <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </div>
  );
}
