# Alpha Markets AI

Edge-detection and risk-sizing engine for prediction markets and sports betting
(Kalshi, Polymarket, sportsbooks). **World Cup beta** — soccer first, more verticals next.

> **What it actually does (read this).** It does **not** predict the future or
> guarantee wins. It finds *mathematical edge*: it strips the vig out of market
> odds, compares the true price to an independent model, flags +EV bets, sizes
> them with the Kelly criterion, and tells you when to cash out. That discipline
> is the only durable edge a retail bettor has. Use it as a co-pilot, not an oracle.

## Quick start

```bash
cd ~/alpha-markets-ai
./run.sh
```

Open **http://localhost:8000**. It runs in **DEMO mode** out of the box (realistic
mock World Cup matches with odds that drift live) — no keys, no cost.

### Go live with real odds
1. Get a free key at <https://the-odds-api.com> (500 requests/month free).
2. `cd backend && cp .env.example .env`, set `ODDS_API_KEY=...` and `DEMO_MODE=false`.
3. Restart. The live feed replaces demo data automatically.

## The model (soccer)
Elo team ratings → expected goal supremacy → per-side expected goals → Poisson
scoreline matrix → P(win/draw/loss), over/under 2.5, both-teams-to-score.
Ratings update as real results come in (`POST /api/result`).

## Features
- **Live Board** — every match with the model's probabilities vs the market,
  ranked value bets tiered **Safe / Mid / Risky**, with edge, EV, and Kelly stake.
- **Combo Builder** — multi-leg parlays: combined true probability, payout, and
  whether the combo is +EV (most aren't — it'll tell you the truth).
- **Cash-Out Checker** — for positions you hold: hold / take profit / bail, with a
  "tables turned" alert when your side's fair value collapses.

## Architecture
```
backend/
  app/
    config.py            settings + env
    probability.py       vig removal, EV, Kelly, odds conversions  (pure math)
    soccer_model.py      Elo → Poisson model ("the AI")
    data_sources/
      odds_api.py        The Odds API client + demo fixtures
    analysis.py          model+market -> tiers, combos, cash-out logic
    main.py              FastAPI routes + serves the frontend
  static/                no-build frontend (index.html / styles.css / app.js)
```

## Roadmap (next verticals)
Basketball / tennis / cricket models → crypto & meme coins (momentum + volatility)
→ stocks/IPOs → options helper (calls/puts: implied vol, breakeven, payoff). See the
`13-Alpha-Markets-AI` tab in Ashar's Brain.

---
*For 18+/21+ where legal. Bet only what you can afford to lose.*
