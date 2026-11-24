import type { BoardResponse, BoardCard } from "./types";

const base: BoardCard = {
  id: "c1", venue: "kalshi", ticker: "KXNFLGAME-26NOV23CINWAS-CIN", series: "KXNFLGAME", game_id: "2026_11_CIN_WAS",
  matchup: "Bengals at Commanders", kickoff: "2026-11-24T01:15:00+00:00", selection: "Bengals", side: "yes",
  market_type: "moneyline", settlement: "Pays $1 if the Bengals win.", price: 0.25, hit: 0.31, raw_hit: 0.40,
  market_prob: 0.24, edge_cents: 4.6, max_entry_price: 0.28, robust: true,
  reasons: ["QB injury not priced", "line moved 2 pts our way"], missing: [], warnings: [],
  evidence: { state: "PROVEN", calls: 212, clusters: 60, reason: "Beats the market on 60 games.",
    model_log_loss: 0.6, market_log_loss: 0.62, roi_after_fees: 0.04 },
  stake: 40, contracts: 100, model_version: "v9",
  trade: { url: "https://kalshi.com/markets/kxnflgame", in_app: false },
};

export const card = (over: Partial<BoardCard> = {}): BoardCard => ({ ...base, ...over });

export const watching = (id: string, matchup: string): BoardCard => card({
  id, matchup, ticker: `KX-${id}`, stake: 0, edge_cents: 2.1,
  evidence: { ...base.evidence, state: "WATCHING", calls: 14, clusters: 14, reason: "Only 14 games settled (needs 40)." },
});

export const board = (cards: BoardCard[]): BoardResponse => ({
  season: 2026, week: 11, built_at: Date.now() / 1000, cards,
  counts: {
    PROVEN: cards.filter((c) => c.evidence.state === "PROVEN").length,
    WATCHING: cards.filter((c) => c.evidence.state === "WATCHING").length,
    FAILING: cards.filter((c) => c.evidence.state === "FAILING").length,
  },
  thresholds: { min_calls: 150, min_clusters: 40 }, empty_reason: null,
});
