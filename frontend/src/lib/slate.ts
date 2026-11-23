export interface SlateGame {
  game_id: string; home: string; away: string; kickoff: string;
  completed: boolean; home_score: number | null; away_score: number | null;
}
export interface Slate { season: number; week: number; built_at: number; games: { game: SlateGame; [k: string]: unknown }[] }

export const slateAsOf = (s: Slate) => s.built_at ?? null;
export const kickoffTime = (iso: string) =>
  new Date(iso).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
