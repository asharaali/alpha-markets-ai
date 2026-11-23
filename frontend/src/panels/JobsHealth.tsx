import { Panel } from "../components/Panel";
import { useApi } from "../lib/api";

interface Jobs { enabled: boolean; jobs: Record<string, { runs: number; errors: number; last_run: number | null; last_error: string | null }> }

/** Configured intervals (render.yaml); the endpoint does not report them. */
const INTERVAL_SEC: Record<string, number> = { market_snapshots: 600, grading: 1800 };
const DEFAULT_INTERVAL = 3600;

const age = (sec: number) => (sec < 3600 ? `${Math.round(sec / 60)}m` : `${(sec / 3600).toFixed(1)}h`);

export function JobsHealth() {
  const { data, status, asOf, error } = useApi<Jobs>("/status/jobs", {
    maxAgeSec: 1200, refreshSec: 120,
    asOf: (d) => Math.max(0, ...Object.values(d.jobs).map((j) => j.last_run ?? 0)) || null,
  });
  const now = Date.now() / 1000;
  return (
    <Panel title="Background jobs" source="scheduler" status={status} asOf={asOf} error={error}>
      <table className="table">
        <thead><tr><th>Job</th><th className="r">Last run</th><th className="r">Errors</th><th>State</th></tr></thead>
        <tbody>{Object.entries(data?.jobs ?? {}).map(([name, j]) => {
          const since = j.last_run ? now - j.last_run : Infinity;
          const stale = since > 3 * (INTERVAL_SEC[name] ?? DEFAULT_INTERVAL);
          return (
            <tr key={name}>
              <td className="mono">{name}</td>
              <td className="num r">{Number.isFinite(since) ? age(since) : "never"}</td>
              <td className="num r">{j.errors}</td>
              <td className={stale || j.last_error ? "verdict-bad" : "verdict-good"}>{j.last_error ? "ERROR" : stale ? "STALE" : "OK"}</td>
            </tr>
          );
        })}</tbody>
      </table>
    </Panel>
  );
}
