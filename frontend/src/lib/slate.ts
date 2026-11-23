export interface SlateGame {
  game_id: string; home: string; away: string; kickoff: string;
  completed: boolean; home_score: number | null; away_score: number | null;
}
export interface Slate { season: number; week: number; built_at: number; games: { game: SlateGame; [k: string]: unknown }[] }

export const slateAsOf = (s: Slate) => s.built_at ?? null;
export const kickoffTime = (iso: string) =>
  new Date(iso).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });

export interface Projection {
  game_id: string; home: string; away: string; kickoff: string; expected_margin: number;
  projected_score?: { home: number; away: number };
  win_probability: { home: number; tie: number; away: number };
}
export type SlateEntry = Slate["games"][number] & { projection?: Projection };
export const pct = (p: number) => `${Math.round(p * 100)}%`;
