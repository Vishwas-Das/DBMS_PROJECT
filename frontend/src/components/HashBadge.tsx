import { useState } from "react";

/** Shows a hash in full (never truncated) with an optional copy button. */
export function HashBadge({ label, value }: { label: string; value: string | null | undefined }) {
  const [copied, setCopied] = useState(false);
  if (!value) {
    return (
      <div className="text-sm">
        <span className="font-medium">{label}:</span> <span className="text-slate-500">-</span>
      </div>
    );
  }
  const copy = () => {
    void navigator.clipboard?.writeText(value).then(
      () => setCopied(true),
      () => setCopied(false),
    );
  };
  return (
    <div className="text-sm">
      <span className="font-medium">{label}:</span>{" "}
      <code
        className="break-all rounded bg-slate-100 px-1 py-0.5 font-mono text-xs"
        data-testid={`hash-${label}`}
      >
        {value}
      </code>{" "}
      <button
        type="button"
        className="ml-1 text-xs text-blue-700 underline"
        onClick={copy}
        aria-label={`Copy ${label}`}
      >
        {copied ? "copied" : "copy"}
      </button>
    </div>
  );
}
