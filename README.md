# Alpha Markets

A quantitative research terminal for NFL prediction markets. It builds an opponent-adjusted
model of every team from play-by-play, prices every Kalshi NFL contract against it, and
shows you the three numbers that matter side by side:

```
Model probability   64.2%
Market probability  56.0%
Estimated edge      +8.2%
```

The research side needs **no API keys and no accounts**. nflverse, Kalshi market data and
Open-Meteo are all free and public. Keys are only required to place real orders.

---

## What it actually does

**Ingests NFL data.** Schedules, play-by-play, injury reports, depth charts, weekly rosters
and per-player game logs from [nflverse](https://github.com/nflverse/nflverse-data); kickoff
weather from Open-Meteo. All free, all versioned, no key.

**Builds opponent-adjusted ratings.** Raw season stats lie — a defence that has faced three
backup quarterbacks looks elite. Every metric (EPA per play, success rate, pressure,
explosiveness, red-zone conversion, turnover rate, neutral-script pace) is fitted so a
team's offence and its opponents' defences are estimated jointly, with ridge shrinkage
handling small samples and recency weighting handling roster turnover.

**Projects games as distributions, not point estimates.** "Chiefs by 4.5" is not a bet.
The model produces a full discrete distribution over margin and total, shaped by a
key-number profile fitted from 4,363 historical games — margins of exactly 3 occur about
2.5× more often than a smooth model implies, which is why a −2.5 and a −3.5 must not be
priced the same.

**Prices real Kalshi contracts.** Kalshi's market list reports every price as `null`; only
the order-book endpoint has real liquidity. Everything here reads the book, filters out
dust levels you cannot actually trade against, and refuses to compute an edge against a
20¢-wide market with no depth behind it.

**Runs several independent strategies.** Moneyline, spread, totals, team totals, winning
margin, matchup, injury impact, situational, line movement, market mispricing and player
props — each with its own methodology, confidence, limitations and track record. An
ensemble combines them in log-odds space, weighted by measured performance once each has
enough settled predictions to have earned it.

**Builds parlays that are priced correctly.** Same-game legs are not independent, and
multiplying their probabilities overstates the parlay. Instead each game is simulated
jointly — margin and total drawn together through their fitted correlation — and every leg
on that game is evaluated against the same simulated scoreline. Nested and contradictory
combinations are refused with an explanation rather than sold.

**Keeps an honest track record.** Every prediction is written to SQLite the moment it is
made, with the price that was showing then, and its outcome column starts empty. Grading
only ever fills the outcome in. No stored probability is ever rewritten, and there is no
code path that removes a losing prediction.

---

## Does the model work?

**Not yet, on the hardest benchmark.** Backtested walk-forward across five seasons against
closing sportsbook lines, its Brier score is 0.2373 against the market's 0.2368. It is
marginally *less* accurate than simply taking the closing price.

That result is reported prominently on the Backtests page rather than buried, and it shaped
the product: the model's blend weights were set by sweeping them against that backtest and
taking the measured region, not by preference. The model is not permitted to override the
price, because the evidence says it has not earned that.

Two things that verdict does **not** say:

- Closing lines are the most efficient prices in sports betting. Very few models beat them.
- This product trades days before kickoff on an exchange with far thinner books than a
  Sunday-morning close. Those are different prices, and Kalshi has no price history to
  backtest against.

So the edge is **unproven**, not disproven. The live tracked record — recorded before
outcomes, un-editable afterwards, with closing-line value measured alongside — is what will
settle it. Size accordingly until it does.

---

## Running it

```bash
./run.sh
```

Then open <http://localhost:8000>.

First start downloads a few seasons of play-by-play (~100MB) and fits the model in a
background job. That takes about a minute; the interface says "calibrating" until it
finishes, and everything else works meanwhile.

Manually:

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

### Tests

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest -q                       # 201 unit tests, no network needed for most
node tests/frontend/smoke.mjs             # renders every view against a running server
```

---

## Configuration

**Nothing is required.** Copy `.env.example` to `.env` and it runs.

| Variable | Needed for | Where to get it |
|---|---|---|
| *(none)* | Research, projections, market data, parlays, backtests | — |
| `KALSHI_KEY_ID`, `KALSHI_PRIVATE_KEY` | Placing **real** orders | kalshi.com → Account → API Keys |
| `LIVE_TRADING_ENABLED`, `LIVE_TRADING_USER` | The other two live-trading gates | You set these |
| `ODDS_API_KEY` | Optional sportsbook cross-check | [the-odds-api.com](https://the-odds-api.com), free tier |

Live orders require **all three** of: the flag on, credentials present, and the logged-in
user matching `LIVE_TRADING_USER`. Any one missing and the order is placed on paper and
labelled as such. `HARD_MAX_STAKE` and `HARD_DAILY_CAP` are ceilings the interface cannot
raise.

Everything else is tunable and documented in `.env.example`.

---

## Architecture

Layers are strictly separated, and every layer boundary speaks in typed interfaces from
`app/core/types.py`.

```
raw data → normalized → features → model → market → strategies → ensemble → recommendations
```

```
backend/app/
  core/         logging, typed errors, retrying HTTP, stale-if-error cache, domain types
  data/         nflverse ingest, streaming CSV, team registry, weather
    kalshi/     API client, order book, series registry + parsers, discovery, execution
  features/     play-by-play → per-team-game aggregates; player usage form
  models/       ridge solver, opponent-adjusted ratings, distributions, calibration
  strategies/   one module per strategy + pricing discipline + ensemble
  parlay/       joint simulation, conflict detection, category builder
  risk/         bankroll, Kelly, exposure limits, drawdown
  tracking/     SQLite store, grading, performance metrics
  backtest/     walk-forward engine with a structural look-ahead guarantee
  jobs/         background calibration, market snapshots, grading
  api/          thin route layer — all of it delegates to app/engine.py
backend/static/ vanilla-JS terminal UI, no build step
```

`app/engine.py` is the single pipeline. The dashboard, the background jobs and the
backtester all run through it, so they cannot disagree about what the model said.

### Three decisions worth knowing about

**No heavy dependencies.** Ridge regression, normal CDFs and the Monte Carlo are pure
Python; play-by-play is read with column-selective streaming CSV. Adding pandas and pyarrow
would have meant a 200MB dependency tree and a wheel-availability gamble on Python 3.14, to
save about a second per season. Reducing 50,000 plays takes 0.9s.

**No frontend build step.** ES modules served directly. The design system is one CSS file
of custom properties; the charts are hand-written inline SVG that inherits the theme.

**The look-ahead guarantee is structural, not a promise.** To predict week N, the backtester
calls the same `ratings.build(season, N)` the live app calls, and that function reads
play-by-play with `max_week = N-1`. There is no code path by which a week-N result reaches a
week-N prediction. `tests/test_look_ahead.py` asserts it — including a control test that
fails if truncation ever silently stops having an effect.

---

## Data sources

| Source | Used for | Key |
|---|---|---|
| [nflverse-data](https://github.com/nflverse/nflverse-data) | Schedules, play-by-play, injuries, depth charts, rosters, player game logs | No |
| [Kalshi](https://kalshi.com) trade API | Market discovery, order books, execution | Read: no. Trade: yes |
| [Open-Meteo](https://open-meteo.com) | Kickoff weather for outdoor games | No |
| [The Odds API](https://the-odds-api.com) | Optional sportsbook cross-check | Yes, optional |

---

## Limitations

Stated plainly, because a research tool that oversells itself is worse than none.

- **The model has not beaten closing lines in backtest.** See above.
- **Player props are reference-only** unless the player is a confirmed starter with measured
  history and no injury designation — and even then are held to roughly double the edge
  threshold of a game line and capped at medium confidence. The model has no access to game
  plans or snap counts for the coming week.
- **Injury positional values are public priors, not fitted here.** The feed carries a
  player's status, not the quality of his replacement.
- **Line movement needs accumulated history.** A freshly deployed instance has none, and
  that strategy correctly stays silent until snapshots build up.
- **Kalshi NFL books are thin**, especially early in the week. Many contracts have no
  tradeable price at all, and the app shows them as untradeable rather than inventing a mid.
- **Backtest coefficients are fitted across the same seasons they are evaluated on.** The
  ratings are strictly walk-forward; the second-stage fit is not, so there is a small
  in-sample advantage baked into the reported RMSE.

---

## Disclaimer

This is research software for analysing prediction markets. Nothing here is financial
advice. A high model edge does not guarantee a winning bet — these are probability
estimates from a model that is wrong some of the time, and every position can lose. Bet
only what you can afford to lose, and only where it is legal for you to do so.
