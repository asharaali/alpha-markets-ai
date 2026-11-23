import { Panel } from "../components/Panel";
import { useApi } from "../lib/api";
import { kickoffTime, pct, slateAsOf, type Slate, type SlateEntry } from "../lib/slate";

export function NflSlate() {
  const { data, status, asOf, error } = useApi<Slate>("/slate", { maxAgeSec: 3600, asOf: slateAsOf, refreshSec: 300 });
  const games = (data?.games ?? []) as SlateEntry[];
  return (
    <Panel title={data ? `NFL week ${data.week}` : "NFL slate"} source="nflverse · model" status={status} asOf={asOf} error={error}>
      <table className="table">
        <thead><tr><th>Game</th><th>When / final</th><th className="r">Home win</th></tr></thead>
        <tbody>{games.map(({ game: g, projection: p }) => (
          <tr key={g.game_id} className={g.completed ? "done" : undefined}>
            <td className="mono">{g.away} @ {g.home}</td>
            <td className="mono">{g.completed ? `${g.away_score}–${g.home_score} F` : kickoffTime(g.kickoff)}</td>
            <td className="num r">{p ? pct(p.win_probability.home) : "—"}</td>
          </tr>
        ))}</tbody>
      </table>
    </Panel>
  );
}
