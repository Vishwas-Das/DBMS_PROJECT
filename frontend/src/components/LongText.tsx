/** Long server text: first part inline, the full text in a disclosure (never truncated away). */
export function LongText({ text, limit = 140 }: { text: string | null | undefined; limit?: number }) {
  if (!text) return <span className="text-slate-500">-</span>;
  if (text.length <= limit) return <span className="break-words">{text}</span>;
  const cut = text.lastIndexOf(" ", limit);
  const head = text.slice(0, cut > limit / 2 ? cut : limit);
  return (
    <details className="break-words">
      <summary className="cursor-pointer">
        {head}... <span className="link text-xs">(show all)</span>
      </summary>
      <span className="text-xs">{text}</span>
    </details>
  );
}
