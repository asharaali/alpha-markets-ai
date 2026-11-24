import { useState } from "react";
import { Panel } from "../components/Panel";
import type { ApiState } from "../lib/api";
import { famName, type EvidenceResponse } from "../research/types";

const pct = (p: number) => `${Math.round(p * 100)}%`;

export function Calibration({ state }: { state: ApiState<EvidenceResponse> }) {
  const { data, status, asOf, error } = state;
  const fams = (data?.families ?? []).filter((f) => f.calibration.length > 0);
  const [pick, setPick] = useState<string | null>(null);
  const fam = fams.find((f) => famName(f) === pick) ?? fams[0];
  return (
    <Panel title="Calibration" source="what each band of calls actually hit" status={status} asOf={asOf} error={error}
      actions={fams.length > 0 && (
        <select aria-label="Family" className="mini-select" value={fam ? famName(fam) : ""} onChange={(e) => setPick(e.target.value)}>
          {fams.map((f) => <option key={famName(f)}>{famName(f)}</option>)}
        </select>)}>
      {!fam ? <p className="muted">No settled calls yet.</p> : (
        <table className="table">
          <thead><tr><th>Band</th><th className="r">Calls</th><th className="r">Said</th><th className="r">Hit</th><th>Gap</th></tr></thead>
          <tbody>{fam.calibration.map((c) => {
            const gap = c.observed - c.predicted;
            return (
              <tr key={c.bucket}>
                <td className="mono">{c.bucket}</td><td className="num r">{c.n}</td>
                <td className="num r">{pct(c.predicted)}</td><td className="num r">{pct(c.observed)}</td>
                <td><span className="gap-bar" style={{ width: `${Math.min(Math.abs(gap) * 400, 100)}%` }} data-sign={gap < 0 ? "neg" : "pos"} /></td>
              </tr>
            );
          })}</tbody>
        </table>
      )}
      <p className="footnote">The board shows the "Hit" column, shrunk toward the market price where a band is thin.</p>
    </Panel>
  );
}
