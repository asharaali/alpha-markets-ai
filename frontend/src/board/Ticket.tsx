import { useEffect, useState } from "react";
import { apiPost } from "../lib/api";
import { cents } from "./format";
import type { BoardCard } from "./types";

interface Preflight { ok: boolean; cost: number | null; contracts: number; estimated_fee: number; ev_after_fees: number | null; message: string | null }

/** In-app order ticket (only when live trading is allowed on this instance, or for paper). */
export function Ticket({ card, onClose }: { card: BoardCard; onClose: () => void }) {
  const [contracts, setContracts] = useState(Math.max(1, Math.min(card.contracts || 10, 50)));
  const [mode, setMode] = useState<"paper" | "live">("paper");
  const [pre, setPre] = useState<Preflight | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const stake = +(contracts * card.price).toFixed(2);

  useEffect(() => {
    let alive = true;
    setPre(null);
    apiPost<Preflight>("/orders/preflight", {
      ticker: card.ticker, side: card.side, stake, model_prob: card.hit,
      recommended_cost: card.price, max_entry_price: card.max_entry_price,
    }).then((p) => alive && setPre(p)).catch((e) => alive && setError(e.message));
    return () => { alive = false; };
  }, [card, stake]);

  const confirm = async () => {
    try {
      const fill = await apiPost<{ status?: string; message?: string }>("/orders", {
        ticker: card.ticker, side: card.side, stake, mode, model_prob: card.hit,
        recommended_cost: card.price, max_entry_price: card.max_entry_price,
        recommendation_id: card.id, game_id: card.game_id, market_type: card.market_type,
        selection: card.selection, label: card.selection,
      });
      await apiPost("/journal", { card, price: pre?.cost ?? card.price, contracts, note: `${mode} order` });
      setDone(fill.message ?? `${mode} order sent`);
    } catch (e) { setError((e as Error).message); }
  };

  return (
    <div className="modal" role="dialog" aria-label="Order ticket">
      <div className="modal-card">
        <h3>{card.selection} · {card.side.toUpperCase()}</h3>
        <p className="muted">{card.matchup} · {card.ticker}</p>
        <label>Contracts <input type="number" min={1} value={contracts} onChange={(e) => setContracts(Math.max(1, +e.target.value || 1))} /></label>
        <label>Mode <select value={mode} onChange={(e) => setMode(e.target.value as "paper" | "live")}>
          <option value="paper">Paper</option><option value="live">Live</option></select></label>
        <p className="num">Stake ${stake.toFixed(2)} · max entry {cents(card.max_entry_price)}</p>
        {pre === null && !error && <p className="muted">Checking the live book…</p>}
        {pre && <p className={pre.ok ? "good" : "bad"}>{pre.ok
          ? `Live price ${cents(pre.cost ?? card.price)} · fee $${pre.estimated_fee.toFixed(2)}`
          : pre.message ?? "Edge is gone at the live price."}</p>}
        {error && <p className="bad">{error}</p>}
        {done ? <p className="good">{done}</p> : (
          <div className="row">
            <button onClick={onClose}>Cancel</button>
            <button className="primary" disabled={!pre?.ok} onClick={confirm}>Confirm {mode}</button>
          </div>
        )}
        {done && <button onClick={onClose}>Close</button>}
      </div>
    </div>
  );
}
