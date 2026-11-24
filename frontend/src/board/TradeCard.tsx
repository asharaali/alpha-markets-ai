import { kickoffTime } from "../lib/slate";
import { cents, edge, pct } from "./format";
import type { BoardCard } from "./types";

const MARK = { PROVEN: "●", WATCHING: "○", FAILING: "✕" } as const;

export function TradeCard({ card, onTrade }: { card: BoardCard; onTrade: (c: BoardCard) => void }) {
  const ev = card.evidence;
  return (
    <article className={`trade-card state-${ev.state.toLowerCase()}`}>
      <header>
        <h3>{card.matchup}</h3>
        <span className="muted">{card.selection} · {card.side.toUpperCase()} · Kalshi{card.kickoff ? ` · ${kickoffTime(card.kickoff)}` : ""}</span>
      </header>
      <div className="figures">
        <span className="fig" title={`Model's raw estimate: ${pct(card.raw_hit)}. Shown: what calls like this have actually hit.`}>
          <em>HIT</em> <b className="num">{pct(card.hit)}</b></span>
        <span className="fig"><em>PRICE</em> <b className="num">{cents(card.price)}</b></span>
        <span className="fig"><em>EDGE</em> <b className="num good">{edge(card.edge_cents)}</b> <small>after fees</small></span>
        <span className={`evidence-chip ev-${ev.state.toLowerCase()}`}>{MARK[ev.state]} {ev.state} ({ev.calls} calls)</span>
      </div>
      {card.reasons.length > 0 && <p className="why"><em>why:</em> <span>{card.reasons.join(" · ")}</span></p>}
      <p className="record">
        <span>{ev.reason}</span>
        {ev.roi_after_fees != null && <span className="num"> · ROI after fees {(ev.roi_after_fees * 100).toFixed(1)}%</span>}
        <span className="num"> · max entry {cents(card.max_entry_price)}</span>
        {card.stake > 0 ? <span className="num"> · stake ${card.stake.toFixed(0)}</span> : <span> · no stake until proven</span>}
      </p>
      {card.missing.length > 0 && <p className="missing">{card.missing.join(" ")}</p>}
      <button className="trade-btn" onClick={() => onTrade(card)}>TRADE</button>
    </article>
  );
}
