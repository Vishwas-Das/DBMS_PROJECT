import type { Bar } from "../lib/format";

/** Plain SVG horizontal bar chart. Values are printed next to each bar. */
export function BarChart({ title, bars, unit }: { title: string; bars: Bar[]; unit?: string }) {
  if (bars.length === 0) return null;
  const max = Math.max(...bars.map((b) => Math.abs(b.value)), 1);
  const rowH = 22;
  const labelW = 190;
  const chartW = 360;
  const valueW = 90;
  const h = bars.length * rowH + 8;
  return (
    <figure className="card">
      <figcaption className="mb-2 text-sm font-medium">{title}</figcaption>
      <svg
        role="img"
        aria-label={`${title}: ${bars.map((b) => `${b.label} ${b.value}`).join("; ")}`}
        width={labelW + chartW + valueW}
        height={h}
        className="max-w-full"
      >
        {bars.map((b, i) => {
          const w = Math.max(1, (Math.abs(b.value) / max) * chartW);
          const y = i * rowH + 4;
          return (
            <g key={b.id}>
              <text x={labelW - 6} y={y + 13} textAnchor="end" fontSize="11" fill="#334155">
                {b.label}
              </text>
              <rect x={labelW} y={y} width={w} height={rowH - 6} fill="#2563eb" />
              <text x={labelW + w + 4} y={y + 13} fontSize="11" fill="#0f172a">
                {b.value}
                {unit ? ` ${unit}` : ""}
              </text>
            </g>
          );
        })}
      </svg>
    </figure>
  );
}
