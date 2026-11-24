export type EvidenceState = "PROVEN" | "WATCHING" | "FAILING";

export interface CardEvidence {
  state: EvidenceState; calls: number; clusters: number; reason: string;
  model_log_loss: number | null; market_log_loss: number | null; roi_after_fees: number | null;
}

export interface BoardCard {
  id: string; venue: "kalshi"; ticker: string; series: string; game_id: string; matchup: string;
  kickoff: string | null; selection: string; side: "yes" | "no"; market_type: string; settlement: string;
  price: number; hit: number; raw_hit: number; market_prob: number | null; edge_cents: number;
  max_entry_price: number; robust: boolean; reasons: string[]; missing: string[]; warnings: string[];
  evidence: CardEvidence; stake: number; contracts: number; model_version: string | null;
  trade: { url: string; in_app: boolean };
}

export interface BoardResponse {
  season: number; week: number; built_at: number; cards: BoardCard[];
  counts: Record<EvidenceState, number>; thresholds: { min_calls: number; min_clusters: number };
  empty_reason: string | null;
}
