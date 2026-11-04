import { useState, type FormEvent } from "react";
import { clearCredentials, getCredentials, setCredentials } from "../api/credentials";
import { Modal } from "./Modal";

/** Operator token and actor name. Stored in sessionStorage only. */
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

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setCredentials({ token, actor });
    onClose();
  };

  return (
    <form onSubmit={submit} className="space-y-3">
      <p className="text-sm text-slate-600">
        Mutating requests send <code>X-LAE-Token</code> and <code>X-LAE-Actor</code>. The token
        stays in this browser tab&apos;s session storage and is cleared when the tab closes.
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
          Actor name (optional, default local-operator)
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
