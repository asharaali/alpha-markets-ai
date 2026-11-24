import { Panel } from "../components/Panel";
import { useApi } from "../lib/api";

interface Entry { id: string; created_at: number; ticker: string; side: string; price: number; contracts: number; note: string;
  card: { matchup?: string; selection?: string; evidence?: { state?: string } }; outcome: "won" | "lost" | "open"; pnl: number | null }
interface JournalResponse { entries: Entry[]; summary: Record<string, { trades: number; won: number; lost: number; open: number; pnl: number }> }

export function Journal() {
  const { data, status, asOf, error } = useApi<JournalResponse>("/journal", { maxAgeSec: 3600, asOf: () => Date.now() / 1000, refreshSec: 600 });
  const summary = Object.entries(data?.summary ?? {});
  return (
    <Panel title="Decision journal" source="trades logged from the board" status={status} asOf={asOf} error={error}>
      {summary.length > 0 && (
        <table className="table"><thead><tr><th>When it was…</th><th className="r">Trades</th><th className="r">W–L</th><th className="r">Open</th><th className="r">P&amp;L</th></tr></thead>
          <tbody>{summary.map(([state, s]) => (
            <tr key={state}><td className={`ev-${state.toLowerCase()} mono`}>{state}</td><td className="num r">{s.trades}</td>
              <td className="num r">{s.won}–{s.lost}</td><td className="num r">{s.open}</td>
              <td className={`num r ${s.pnl >= 0 ? "good" : "bad"}`}>{s.pnl >= 0 ? "+" : ""}${s.pnl.toFixed(2)}</td></tr>
          ))}</tbody></table>
      )}
      {data && data.entries.length === 0 && <p className="muted">No trades logged yet. Every TRADE from the board lands here with the card it came from.</p>}
      <ul className="journal">{(data?.entries ?? []).slice(0, 20).map((e) => (
        <li key={e.id}><span className="mono">{new Date(e.created_at * 1000).toLocaleDateString()}</span>{" "}
          {e.card.selection ?? e.ticker} {e.side.toUpperCase()} · {e.contracts} @ {Math.round(e.price * 100)}¢ ·{" "}
          <b className={e.outcome === "won" ? "good" : e.outcome === "lost" ? "bad" : "muted"}>{e.outcome.toUpperCase()}</b>
          {e.pnl != null && <span className="num"> {e.pnl >= 0 ? "+" : ""}${e.pnl.toFixed(2)}</span>}
          {e.note && <span className="muted"> — {e.note}</span>}</li>
      ))}</ul>
    </Panel>
  );
}
