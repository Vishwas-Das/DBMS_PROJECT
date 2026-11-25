import { useMemo, useState } from "react";
import { useBenchmarks } from "../api/hooks";
import type { BenchmarkRunOut } from "../api/types";
import { BarChart } from "../components/BarChart";
import { EmptyState, QueryState } from "../components/States";
import { formatDateTime, metricBars, numericMetricKeys, prettyJson, shortId } from "../lib/format";

function Charts({ runs }: { runs: BenchmarkRunOut[] }) {
  const keys = useMemo(() => numericMetricKeys(runs), [runs]);
  const [metric, setMetric] = useState<string>("");
  const active = metric && keys.includes(metric) ? metric : (keys[0] ?? "");
  if (keys.length === 0) {
    return <EmptyState title="No numeric top-level metrics to chart" />;
  }
  return (
    <div className="space-y-2">
      <div>
        <label htmlFor="metric-select" className="label">
          Metric
        </label>
        <select
          id="metric-select"
          className="input max-w-xs"
          value={active}
          onChange={(e) => setMetric(e.target.value)}
        >
          {keys.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
      </div>
      <BarChart
        title={`${active} per run`}
        bars={metricBars(runs, active)}
        unit={active.endsWith("_ms") ? "ms" : undefined}
      />
    </div>
  );
}

export function BenchmarksPage() {
  const [scenario, setScenario] = useState("");
  const q = useBenchmarks(scenario.trim() || null);
  return (
    <div className="space-y-4">
      <h1 className="page-title">Benchmarks</h1>
      <div>
        <label htmlFor="scenario-filter" className="label">
          Scenario filter (exact, e.g. B1)
        </label>
        <input
          id="scenario-filter"
          className="input max-w-xs"
          value={scenario}
          onChange={(e) => setScenario(e.target.value)}
        />
      </div>
      <QueryState
        query={q}
        errorTitle="Could not load benchmark runs"
        isEmpty={(r) => r.length === 0}
        empty={
          <EmptyState title="No benchmark runs stored">
            Run <code>python -m benchmarks.run --seed 42</code> to record some.
          </EmptyState>
        }
      >
        {(runs) => (
          <div className="space-y-4">
            <Charts runs={runs} />
            <table className="table" aria-label="Benchmark runs">
              <thead>
                <tr>
                  <th scope="col">Run</th>
                  <th scope="col">Scenario</th>
                  <th scope="col">Seed</th>
                  <th scope="col">Commit</th>
                  <th scope="col">Created</th>
                  <th scope="col">Params</th>
                  <th scope="col">Metrics</th>
                  <th scope="col">Environment</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.id}>
                    <td className="font-mono text-xs">{shortId(r.id)}</td>
                    <td>{r.scenario}</td>
                    <td>{r.seed}</td>
                    <td className="font-mono text-xs">{r.git_commit?.slice(0, 10) ?? "-"}</td>
                    <td className="whitespace-nowrap">{formatDateTime(r.created_at)}</td>
                    {[r.params, r.metrics, r.environment].map((obj, i) => (
                      <td key={i}>
                        <details>
                          <summary className="cursor-pointer text-xs">
                            {Object.keys(obj).length} keys
                          </summary>
                          <pre className="max-w-sm overflow-auto text-[11px]">{prettyJson(obj)}</pre>
                        </details>
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </QueryState>
    </div>
  );
}
