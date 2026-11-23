// Shapes copied from production responses on 2026-11-23.
const proj = (id: string, home: string, away: string, kickoff: string, margin: number, homeWin: number) => ({
  game_id: id, home, away, kickoff, expected_margin: margin,
  projected_score: { home: 24 + margin / 2, away: 24 - margin / 2 },
  win_probability: { home: homeWin, tie: 0.004, away: 1 - homeWin - 0.004 },
});

export const SLATE_FULL = {
  season: 2026, week: 11, built_at: 1_795_474_548,
  games: [
    { game: { game_id: "2026_11_IND_HOU", home: "HOU", away: "IND", kickoff: "2026-11-20T01:15:00+00:00", completed: true, home_score: 26, away_score: 3 },
      projection: proj("2026_11_IND_HOU", "HOU", "IND", "2026-11-20T01:15:00+00:00", 0.5, 0.51) },
    { game: { game_id: "2026_11_CIN_WAS", home: "WAS", away: "CIN", kickoff: "2026-11-24T01:15:00+00:00", completed: false, home_score: null, away_score: null },
      projection: proj("2026_11_CIN_WAS", "WAS", "CIN", "2026-11-24T01:15:00+00:00", -3.2, 0.40) },
    { game: { game_id: "2026_12_KC_DEN", home: "DEN", away: "KC", kickoff: "2026-11-27T21:25:00+00:00", completed: false, home_score: null, away_score: null },
      projection: proj("2026_12_KC_DEN", "DEN", "KC", "2026-11-27T21:25:00+00:00", 7.5, 0.72) },
  ],
};

export const STRATEGIES = {
  strategies: [
    { key: "moneyline", name: "Moneyline", record: { n: 322, games: 161, meaningful: true, log_loss: 0.3191, log_loss_market: 0.2866, brier: 0.094, brier_market: 0.0884 } },
    { key: "spread", name: "Spread", record: { n: 2836, games: 162, meaningful: true, log_loss: 0.3231, log_loss_market: 0.2931, brier: 0.0973, brier_market: 0.0925 } },
    { key: "situational", name: "Situational", record: { n: 0, meaningful: false, note: "No settled predictions yet." } },
  ],
};

export const jobs = (now: number) => ({
  enabled: true, running: true,
  jobs: {
    grading: { runs: 25, errors: 0, last_run: now - 4 * 3600, last_error: null },
    market_snapshots: { runs: 74, errors: 0, last_run: now - 120, last_error: null },
  },
});
