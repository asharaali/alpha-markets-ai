import { Panel } from "../components/Panel";
import type { ApiState } from "../lib/api";
import type { EvidenceResponse } from "../research/types";
import { famName } from "../research/types";

const f3 = (x: number | null) => (x == null ? "—" : x.toFixed(3));

export function EvidenceTable({ state }: { state: ApiState<EvidenceResponse> }) {
  const { data, status, asOf, error } = state;
  return (
    <Panel title="Evidence gate" source="settled predictions · clustered by game" status={status} asOf={asOf} error={error} className="wide">
      {data && <p className="footnote">PROVEN needs ≥{data.thresholds.min_calls} calls, ≥{data.thresholds.min_clusters} games, interval below 0, ROI after fees &gt; 0.</p>}
      <table className="table">
        <thead><tr><th>Family</th><th>State</th><th className="r">Calls</th><th className="r">Games</th>
          <th className="r">Model LL</th><th className="r">Market LL</th><th className="r">95% interval</th><th className="r">ROI</th><th>Why</th></tr></thead>
        <tbody>{(data?.families ?? []).map((f) => (
          <tr key={famName(f)}>
            <td className="mono">{famName(f)}</td>
            <td className={`ev-${f.state.toLowerCase()} mono`}>{f.state}</td>
            <td className="num r">{f.calls}</td>
            <td className="num r">{f.clusters}</td>
            <td className="num r">{f3(f.model_log_loss)}</td>
            <td className="num r">{f3(f.market_log_loss)}</td>
            <td className="num r">{f.ci95 ? `${f.ci95[0].toFixed(3)} … ${f.ci95[1].toFixed(3)}` : "—"}</td>
            <td className="num r">{f.roi_after_fees == null ? "—" : `${(f.roi_after_fees * 100).toFixed(1)}%`}</td>
            <td className="muted">{f.reason}</td>
          </tr>
        ))}</tbody>
      </table>
    </Panel>
  );
}
