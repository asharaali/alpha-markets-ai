import { Panel } from "../components/Panel";
import { useApi } from "../lib/api";

interface Record_ { n: number; games?: number; meaningful: boolean; log_loss?: number; log_loss_market?: number }
interface Strategies { strategies: { key: string; name?: string; record?: Record_ }[] }

/** The honest-record panel: green only when a strategy actually beats the market price. */
export function ModelVsMarket() {
  // Track records change at grading time (every 30 min); no timestamp in the payload, so
  // freshness is the fetch itself.
  const { data, status, asOf, error } = useApi<Strategies>("/strategies",
    { maxAgeSec: 3600, asOf: () => Date.now() / 1000, refreshSec: 1800 });
  const rows = (data?.strategies ?? []).filter((s) => s.record?.meaningful && s.record.log_loss != null);
  return (
    <Panel title="Model vs market" source="settled predictions · log loss (lower wins)" status={status} asOf={asOf} error={error}>
      <table className="table">
        <thead><tr><th>Strategy</th><th className="r">Games</th><th className="r">Model</th><th className="r">Market</th><th>Verdict</th></tr></thead>
        <tbody>{rows.map((s) => {
          const r = s.record!; const beats = r.log_loss! < r.log_loss_market!;
          return (
            <tr key={s.key}>
              <td>{s.name ?? s.key}</td>
              <td className="num r">{r.games ?? "—"}</td>
              <td className="num r">{r.log_loss!.toFixed(3)}</td>
              <td className="num r">{r.log_loss_market!.toFixed(3)}</td>
              <td className={beats ? "verdict-good" : "verdict-bad"}>{beats ? "BEATS MARKET" : "WORSE THAN MARKET"}</td>
            </tr>
          );
        })}</tbody>
      </table>
      {rows.length > 0 && rows.every((s) => s.record!.log_loss! >= s.record!.log_loss_market!) && (
        <p className="footnote">No strategy beats the market price yet. Nothing here earns a recommendation.</p>
      )}
    </Panel>
  );
}
