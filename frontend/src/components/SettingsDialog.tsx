import { useState, type FormEvent } from "react";
import { clearCredentials, getCredentials, setCredentials } from "../api/credentials";
import { Modal } from "./Modal";

/** Event that asks the layout to open the Settings dialog (used by 401 error states). */
export const OPEN_SETTINGS_EVENT = "lae:open-settings";

export function requestOpenSettings(): void {
  window.dispatchEvent(new Event(OPEN_SETTINGS_EVENT));
}

/** Operator token and actor names. Stored in sessionStorage only. */
export function SettingsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Modal open={open} title="API settings" onClose={onClose}>
      {open ? <SettingsForm onClose={onClose} /> : null}
    </Modal>
  );
}

function SettingsForm({ onClose }: { onClose: () => void }) {
  const initial = getCredentials();
  const [token, setToken] = useState(initial.token ?? "");
  const [actor, setActor] = useState(initial.actor ?? "");
  const [executor, setExecutor] = useState(initial.executor ?? "");

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setCredentials({ token, actor, executor });
    onClose();
  };

  return (
    <form onSubmit={submit} className="space-y-3">
      <p className="text-sm text-slate-600">
        Every API call except the health check sends <code>X-LAE-Token</code> and{" "}
        <code>X-LAE-Actor</code>. The token stays in this browser tab&apos;s session storage and is
        cleared when the tab closes.
      </p>
      <div>
        <label htmlFor="settings-token" className="label">
          API token (LAE_API_TOKEN)
        </label>
        <input
          id="settings-token"
          type="password"
          autoComplete="off"
          className="input"
          value={token}
          onChange={(e) => setToken(e.target.value)}
        />
      </div>
      <div>
        <label htmlFor="settings-actor" className="label">
          Approver name (X-LAE-Actor for approval and all other calls)
        </label>
        <input
          id="settings-actor"
          type="text"
          maxLength={64}
          className="input"
          value={actor}
          onChange={(e) => setActor(e.target.value)}
        />
      </div>
      <div>
        <label htmlFor="settings-executor" className="label">
          Executor name (X-LAE-Actor for dry runs and LIVE execution)
        </label>
        <input
          id="settings-executor"
          type="text"
          maxLength={64}
          className="input"
          aria-describedby="settings-executor-help"
          value={executor}
          onChange={(e) => setExecutor(e.target.value)}
        />
        <p id="settings-executor-help" className="mt-1 text-xs text-slate-600">
          Four-eyes rule: the server rejects a LIVE execution by the same actor who approved the
          plan (separation of duties). Use a different name here than the approver. When empty, the
          approver name is sent.
        </p>
      </div>
      <div className="flex justify-end gap-2">
        <button
          type="button"
          className="btn-secondary"
          onClick={() => {
            clearCredentials();
            onClose();
          }}
        >
          Clear
        </button>
        <button type="button" className="btn-secondary" onClick={onClose}>
          Cancel
        </button>
        <button type="submit" className="btn-primary">
          Save
        </button>
      </div>
    </form>
  );
}
