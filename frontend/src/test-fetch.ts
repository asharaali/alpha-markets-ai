import { vi } from "vitest";

/** Stub fetch with a route table: path → [status, body]. Records every URL requested. */
export function stubFetch(routes: Record<string, [number, unknown] | (() => [number, unknown])>) {
  const calls: string[] = [];
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push(url);
    const key = `${init?.method ?? "GET"} ${url}`;
    const hit = routes[key] ?? routes[url];
    if (!hit) return new Response(JSON.stringify({ error: "not_found" }), { status: 404 });
    const [status, body] = typeof hit === "function" ? hit() : hit;
    return new Response(JSON.stringify(body), { status });
  });
  vi.stubGlobal("fetch", fn);
  return { calls, fn };
}

export const SLATE = {
  season: 2026, week: 11, built_at: 1_795_474_548,
  games: [
    { game: { game_id: "2026_11_IND_HOU", home: "HOU", away: "IND", kickoff: "2026-11-20T01:15:00+00:00",
      completed: true, home_score: 26, away_score: 3 } },
    { game: { game_id: "2026_11_CIN_WAS", home: "WAS", away: "CIN", kickoff: "2026-11-24T01:15:00+00:00",
      completed: false, home_score: null, away_score: null } },
  ],
};
