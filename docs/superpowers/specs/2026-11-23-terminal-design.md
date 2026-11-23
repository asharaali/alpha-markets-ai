# Alpha Markets Terminal — design

Date: 2026-11-23 · Branch: `terminal` · Owner: Ashar (single user)

## Intent

Turn Alpha Markets from a single NFL model into a private, Sorcerer-style multi-market
research terminal. Three reasons, in Ashar's words: **private access only for me**,
**would look good on portfolio**, and **to use for my own trading purposes**.

The centre of it is a **Trade Board**: recommended trades populate with a hit % on what the
model thinks; Ashar opens the dashboard, scans it, and decides "yeah I'll trade this". He
asked explicitly for a research and analysis layer "so the decisions it makes are actually
educated and not just random bs".

Context that shapes everything: on 2026-11-23 the live record (162 graded NFL games) showed
every existing strategy scoring *worse* than the Kalshi price on Brier and log loss, and
overconfident on long shots. The terminal must make that kind of finding impossible to miss.

## Goals

1. Feature parity with sorcerer.money's surface (inspected 2026-11-23): dense multi-panel
   terminal; prediction-market terminal (Kalshi + Polymarket); sports ratings / projections /
   playoff odds / live scores / player ratings / game of the day across NFL, NBA, MLB, NCAAF,
   NCAAB, international soccer; macro (yields, Fed odds, CPI/PCE/PPI/GDP, recession odds, live
   markets, fear & greed); politics (Senate/House/governor, map, generic ballot, approval, poll
   watcher); a P&L panel; research/analysis articles; stat leaders; ticker bar; search.
2. Trade Board with calibrated hit %, price, edge after fees, reasons, sources, and the
   signal's own track record. A [TRADE] action per card.
3. Research layer (below) that gates what may be called a recommendation.
4. Demo mode a recruiter can open: frozen snapshot data, no balances, positions or account ids.

## Non-goals

- No investment fund, no outside money, no APY targets (securities law; ruled out).
- No subscriptions, paywalls or "subscription required" blur — single user.
- No public picks / "big wins" feed.
- Not a copy of Sorcerer's name, logo, copy text or exact visual design. Same feature set, own
  identity ("Alpha Markets Terminal"), own numbers. Their proprietary outputs are never scraped.

## Anti-goals (failure even if it works)

- A card that shows a high hit % without price and edge beside it.
- An unproven signal presented as a recommendation.
- A hit % that is the model's raw opinion when its history says it is overconfident.
- A Kalshi private key on the public Render box.
- Panels that silently show stale data as if live (every panel carries its as-of time and
  source; stale = visibly marked).

## Research & analysis layer

1. **Evidence gate.** Each signal family (strategy × market type, later per module) is scored
   out-of-sample against the market price at the time of the call, using the existing paired,
   game-clustered comparison (`evaluation/protocol.paired_difference`). States:
   - `PROVEN` — beats market on log loss with the clustered 95% interval excluding zero, over
     ≥ 150 independent settled calls (≥ 40 clusters), and positive after-fee ROI.
   - `WATCHING` — not enough evidence either way.
   - `FAILING` — significantly worse than market. Hidden from the board by default.
   Thresholds live in config, are shown on the Research page, and are never lowered to fill
   the screen. "No proven trades right now" is a valid board.
2. **Calibrated hit %.** Raw model probability is mapped through the signal family's own
   settled calibration (isotonic / binned, shrunk toward the market price when the sample is
   thin). Card shows the calibrated number; raw is on hover.
3. **Show the work.** Every card: venue, contract, price, calibrated hit %, edge after fees,
   max entry price, robustness (existing `is_robust`), reasons, data sources with as-of times,
   and the family's record (n, W–L, ROI, log-loss vs market).
4. **Research briefs.** Claude-written notes generated from the terminal's own data (game
   previews, Fed-meeting previews, race notes), each claim tied to a cited data point; briefs
   that would need facts the data does not have are not written. Model: latest Claude
   (verify id via claude-api docs at build time). Runs server-side on a schedule; off when no
   API key is configured.
5. **Decision journal.** When Ashar trades from a card, record the card snapshot, his note,
   and the outcome; Research page shows which reasons/families actually pay.
6. **Sizing.** Suggested stake scales with evidence state (WATCHING = 0, PROVEN = fractional
   Kelly, existing `KELLY_FRACTION`), capped per trade and per day in config.

## Constraints

- Existing backend: FastAPI + SQLite on Render Starter (0.5 CPU). Anything > 0.5 s CPU off the
  event loop (lesson from the 9/28 health-check kills). Render disk at `/var/data`.
- Live trading stays disabled on the hosted instance (`render.yaml` rationale kept). [TRADE]
  on hosted = deep link to the venue's market page; on the local instance on Ashar's Mac
  (`run.sh`, key installed via `install_kalshi_key.sh`) = pre-filled ticket through the
  existing `data/kalshi/execution.place`, with its limits and edge-still-available check.
- Free/public data sources first (nflverse, Kalshi public data, FRED, Treasury, BLS, ESPN
  public scoreboard JSON, Open-Meteo, Polymarket public API). Paid keys only if a module
  can't be done without one, raised to Ashar before adding.
- Polymarket trading from NJ: availability unverified. Read-only Polymarket data ships
  regardless; trading only after verification.
- Single user; existing auth (closed sign-ups, expiring sessions, lockout) is kept.

## Approaches considered

- **A. Keep backend, rebuild frontend in React + TypeScript (Vite), built to static and served
  by the same FastAPI service.** *Picked.* Reuses ~16k lines of working Python (Kalshi data and
  execution, auth, grading, evaluation); a 30-panel live terminal is far more maintainable as
  typed components; React/TS reads well on a portfolio; one Render service, no new host.
- B. Keep the vanilla-JS SPA and extend it. Cheapest start, but 4k lines of hand-rolled DOM
  will not hold Sorcerer's density; rejected.
- C. Greenfield repo. Throws away the honest-tracking machinery; rejected.

## Modules and order (agreed)

1. **Shell** — layout grid, ticker bar, nav, search, login, demo mode, panel framework
   (each panel: title, status chip LIVE/LOADING/STALE, as-of, source, error state).
2. **Prediction-market terminal + Trade Board + research layer v1** (Kalshi first, then
   Polymarket read-only).
3. **P&L** — unified Kalshi + Public + Topstep, read-only. Public/Topstep credentials live on
   the Mac, so a Mac-side job pushes a P&L snapshot to the service (same pattern as KRYVANT's
   publish job) rather than putting those keys on Render.
4. **Macro & rates.**
5. **Sports** beyond NFL (NBA, MLB, NCAAF, NCAAB, soccer; scores, ratings, projections).
6. **Politics.**

Each module ships behind its own nav entry, with tests, and is deployed when done. This doc
covers the whole terminal; each module gets its own implementation plan when it starts.

## Calls made for Ashar (correct any)

- Product name "Alpha Markets Terminal"; dark, dense, monospace-accented terminal look of our
  own design.
- Proven threshold numbers above (150 calls / 40 clusters / 95% interval).
- Demo mode served at `/demo` from a sanitized snapshot committed to the repo.
- Old vanilla-JS views stay reachable at `/classic` until module 2 replaces them, then deleted.
- Research briefs use the Claude API only if `ANTHROPIC_API_KEY` is set on the service.

## Left to the builder

Component library choice (keep it light; no heavy UI kit), chart library, panel grid
implementation, caching per data source, exact endpoint shapes.
