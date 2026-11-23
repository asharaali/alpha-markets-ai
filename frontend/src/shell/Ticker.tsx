import { useApi } from "../lib/api";
import { kickoffTime, slateAsOf, type Slate } from "../lib/slate";

export function Ticker() {
  const { data, status } = useApi<Slate>("/slate", { maxAgeSec: 3600, asOf: slateAsOf, refreshSec: 300 });
  const games = data?.games.map((g) => g.game) ?? [];
  return (
    <div className="ticker" aria-label="Ticker">
      <span className={`chip chip-${status.toLowerCase()}`}>{data ? `NFL WK ${data.week}` : status}</span>
      <div className="ticker-track">
        {games.map((g) => (
          <span key={g.game_id} className="ticker-item mono">
            {g.completed
              ? <>{g.away} {g.away_score} @ {g.home} {g.home_score} <em>FINAL</em></>
              : <>{g.away} @ {g.home} <em>{kickoffTime(g.kickoff)}</em></>}
          </span>
        ))}
      </div>
    </div>
  );
}
