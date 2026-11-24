export type Kind = "print" | "market" | "quote" | "index";
export interface Indicator { id: string; label: string; group: string; value: number; prev: number | null; change: number | null;
  mom: number | null; yoy: number | null; as_of: string; kind: Kind; source: string }
export interface Quote { symbol: string; label: string; price: number; prev_close: number | null; change_pct: number | null;
  as_of: number; market_open: boolean; kind: Kind; source: string }
export interface FedOdds { event: string; title: string; meeting_date: string; current_upper: number;
  outcomes: { label: string; prob: number }[]; url: string; kind: Kind; source: string }
export interface Odds { label: string; prob: number; url: string; kind: Kind; source: string }
export interface Sentiment { name: string; value: number; label: string; as_of: number; kind: Kind; source: string }
export interface MacroBlob {
  ready: boolean; built_at?: number; errors?: Record<string, string>;
  sections?: {
    rates?: { items: Indicator[] }; inflation?: { items: Indicator[] }; jobs_growth?: { items: Indicator[] };
    fed?: FedOdds; recession?: Odds; markets?: { quotes: Quote[]; fallback: Indicator[] }; sentiment?: Sentiment;
  };
}
export const pct0 = (p: number) => `${Math.round(p * 100)}%`;
export const oddsPct = (p: number) => (p < 0.1 ? `${(p * 100).toFixed(1)}%` : pct0(p));
export const rate = (v: number, d = 2) => `${v.toFixed(d)}%`;
