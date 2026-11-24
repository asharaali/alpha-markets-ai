import { useMemo, useState } from "react";
import { useSearchParams } from "react-router";
import { LogTrade } from "../board/LogTrade";
import { Ticket } from "../board/Ticket";
import { TradeCard } from "../board/TradeCard";
import type { BoardCard, BoardResponse } from "../board/types";
import { Panel } from "../components/Panel";
import { useApi } from "../lib/api";

type Sort = "edge" | "hit" | "kickoff";

export function Board() {
  const { data, status, asOf, error } = useApi<BoardResponse>("/board", { maxAgeSec: 900, asOf: (d) => d.built_at, refreshSec: 300 });
  const [params] = useSearchParams();
  const q = (params.get("q") ?? "").toLowerCase();
  const [showAll, setShowAll] = useState(false);
  const [sort, setSort] = useState<Sort>("edge");
  const [active, setActive] = useState<BoardCard | null>(null);

  const cards = useMemo(() => {
    let rows = data?.cards ?? [];
    if (q) rows = rows.filter((c) => [c.matchup, c.ticker, c.game_id, c.selection].some((s) => s.toLowerCase().includes(q)));
    if (!showAll) rows = rows.filter((c) => c.evidence.state === "PROVEN");
    const key: Record<Sort, (c: BoardCard) => number> = {
      edge: (c) => -c.edge_cents, hit: (c) => -c.hit, kickoff: (c) => (c.kickoff ? Date.parse(c.kickoff) : Infinity),
    };
    return [...rows].sort((a, b) => key[sort](a) - key[sort](b));
  }, [data, q, showAll, sort]);

  const trade = (c: BoardCard) => {
    if (c.trade.in_app) { setActive(c); return; }
    window.open(c.trade.url, "_blank", "noopener");
    setActive(c);
  };
  const watching = data?.counts.WATCHING ?? 0;

  return (
    <div className="board-page">
      <Panel title={data ? `Trade board · NFL week ${data.week}` : "Trade board"} source="ensemble · calibrated · after fees"
        status={status} asOf={asOf} error={error}
        actions={<div className="board-controls">
          <button className={!showAll ? "on" : ""} onClick={() => setShowAll(false)}>Proven</button>
          <button className={showAll ? "on" : ""} onClick={() => setShowAll(true)}>All</button>
          <select aria-label="Sort" value={sort} onChange={(e) => setSort(e.target.value as Sort)}>
            <option value="edge">Edge</option><option value="hit">Hit %</option><option value="kickoff">Kickoff</option>
          </select>
        </div>}>
        {data && (
          <p className="footnote">
            {data.counts.PROVEN} proven · {watching} watching · {data.counts.FAILING} failing. PROVEN needs ≥{data.thresholds.min_calls} calls on
            ≥{data.thresholds.min_clusters} games, beating the market price and profitable after fees.
          </p>
        )}
        {data && cards.length === 0 && !showAll && (
          <div className="empty">
            <p>No proven trades right now. {watching} {watching === 1 ? "idea is" : "ideas are"} being watched.</p>
            <button onClick={() => setShowAll(true)}>Show all</button>
          </div>
        )}
        {data && cards.length === 0 && showAll && <p className="muted">{data.empty_reason ?? "Nothing matches."}</p>}
        <div className="cards">{cards.map((c) => <TradeCard key={c.id + c.ticker} card={c} onTrade={trade} />)}</div>
      </Panel>
      {active && (active.trade.in_app
        ? <Ticket card={active} onClose={() => setActive(null)} />
        : <LogTrade card={active} onClose={() => setActive(null)} />)}
    </div>
  );
}
