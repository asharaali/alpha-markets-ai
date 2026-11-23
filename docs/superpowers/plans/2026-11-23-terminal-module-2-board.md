# Terminal Module 2 — Trade Board, Research Layer, Markets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Trade Board (calibrated hit %, price, edge after fees, evidence state, reasons, one-click trade), the research layer behind it (evidence gate, calibration, decision journal, Claude briefs), and a Markets page reading Kalshi and Polymarket US side by side.

**Architecture:** New backend package `app/research/` (evidence, calibration, board assembly, journal, briefs) on top of the existing settled-prediction store and `evaluation/protocol`; new data clients for Kalshi open events and the Polymarket US public gateway. Frontend adds Board, Research and Markets pages using the Module 1 `Panel`/`useApi`.

**Tech Stack:** as Module 1. Anthropic Python SDK for briefs (only active when `ANTHROPIC_API_KEY` is set).

**Spec:** `docs/superpowers/specs/2026-11-23-terminal-design.md` (sections "Research & analysis layer", "Constraints", Module 2). Module 1 ledger: `docs/superpowers/plans/2026-11-23-terminal-module-1-ledger.md`.

## Global Constraints

- PROVEN = beats market on log loss, clustered (by game) 95% interval of (model − market) entirely below 0, ≥ 150 independent settled calls, ≥ 40 clusters, after-fee ROI > 0. FAILING = interval entirely above 0. Else WATCHING. Thresholds in `app/config.py` (`EVIDENCE_MIN_CALLS=150`, `EVIDENCE_MIN_CLUSTERS=40`) and printed on the Research page. Never lowered to fill a screen.
- A card's hit % is the calibrated probability; raw model probability only on hover. Edge after fees is recomputed from the calibrated hit %, never from the raw one.
- Every card shows price next to hit %. Stake suggestion is 0 unless the family is PROVEN.
- Live trading stays off on Render. `[TRADE]` = in-app ticket only when `/api/me` says `live_trading.allowed`; otherwise a venue link. Paper orders remain available in-app (existing `mode="paper"`).
- Polymarket US: read-only (public gateway `https://gateway.polymarket.us/v1/markets`). Event links `https://polymarket.us/event/<slug>`. Kalshi links `https://kalshi.com/markets/<series_ticker lowercased>`.
- Every `/api/*` route added here is private (Module 1 gate; the route-walker test enforces it).
- Anything > 0.5 s CPU off the event loop (`asyncio.to_thread`), results cached.

## Review Focus

1. A family with thousands of raw rows from repeated refreshes of 14 games → must dedupe to ≤ 14 clusters and stay WATCHING, not PROVEN on volume. (Task 1 test.)
2. Calibration bin with zero settled rows → hit % falls back to market price (or raw when no market price), never NaN. (Task 2 test.)
3. Board card whose family has no evidence at all (new market type) → WATCHING, stake 0, still listed under "watching". (Task 3 test.)
4. Polymarket gateway returns a market with missing/empty prices or an unknown shape → that market is skipped, the page still renders the rest. (Task 4 test.)
5. Briefs requested with no API key, or the API erroring → endpoint returns `{"enabled": false}` / cached-or-empty with a reason, never a 500. (Task 8 test.)

---

### Task 1: Evidence gate

**Files:** Create `backend/app/research/__init__.py`, `backend/app/research/evidence.py`; modify `backend/app/config.py` (two settings); modify `backend/app/api/routes_research.py` (endpoint). Test `backend/tests/test_evidence.py`.

**Interfaces:**
- Consumes: `store.iter_settled_predictions(columns=...)`, `protocol.deduplicate`, `protocol.paired_difference`, `risk.fees` (`fee_model.trading_fee(contracts, price)`).
- Produces:
  - `Family = tuple[str, str]` (strategy, market_type); `family_key(row) -> Family`.
  - `@dataclass Evidence: family: Family; state: Literal["PROVEN","WATCHING","FAILING"]; calls: int; clusters: int; model_log_loss: float|None; market_log_loss: float|None; difference: float|None; ci95: tuple[float,float]|None; roi_after_fees: float|None; reason: str` + `to_dict()`.
  - `assess(rows: Iterable[dict]) -> Evidence` (one family's rows; dedupes first; per-row log loss of `fair_prob or model_prob` and of `market_prob` against `outcome`, clipped to [1e-4, 1-1e-4]; ROI = flat $1 on rows with `ev_per_dollar > 0`, minus `trading_fee(contracts=round(1/cost), price=cost)` per row).
  - `assess_all() -> dict[Family, Evidence]` (streams settled rows, groups, assesses; module-level cache 1800 s).
  - `GET /api/research/evidence` → `{"thresholds": {...}, "families": [Evidence.to_dict(), ...]}` computed via `asyncio.to_thread`.
- `reason` copy: PROVEN `"Beats the market on {clusters} games."`; FAILING `"Significantly worse than the market price."`; WATCHING one of `"Only {clusters} games settled (needs 40)."`, `"Only {calls} calls (needs 150)."`, `"Not distinguishable from the market yet."`, `"Beats the market but loses money after fees."`.

- [ ] Step 1: failing tests — `test_clearly_better_than_market_over_60_games_is_proven` (synthetic: 60 games × 3 markets, outcomes drawn so model p is truth, market p shrunk toward 0.5 by 40%, costs = market p, ev>0 rows profitable → PROVEN); `test_identical_to_market_is_watching`; `test_clearly_worse_is_failing`; `test_refreshes_do_not_count_as_evidence` (14 games × 500 refreshes each of a better model → clusters == 14, state WATCHING, reason mentions "needs 40"); `test_better_but_unprofitable_after_fees_is_watching`; `test_endpoint_is_private_and_lists_thresholds` (401 logged out; logged in returns thresholds 150/40).
- [ ] Step 2: run `cd backend && .venv/bin/python -m pytest tests/test_evidence.py -q` → FAIL (module missing).
- [ ] Step 3: implement. Step 4: run file → PASS, full suite → PASS.
- [ ] Step 5: commit `"Evidence gate: PROVEN / WATCHING / FAILING per strategy and market type"`

### Task 2: Calibration

**Files:** Create `backend/app/research/calibration.py`; test `backend/tests/test_research_calibration.py`.

**Interfaces:**
- Produces: `class Calibrator` with `fit(rows: Iterable[dict]) -> Calibrator` (deduped settled rows of one family; 10 bins of width 0.1 on `fair_prob or model_prob`, counting n and hits), `apply(p: float, market_prob: float | None) -> float`, `table() -> list[dict]` (`bucket`, `n`, `predicted`, `observed`). `SHRINK_K = 30`.
- `apply`: bin b of p with n_b, hit rate o_b; anchor = market_prob if not None else p; return `(n_b * o_b + K * anchor) / (n_b + K)`.
- `calibrators() -> dict[Family, Calibrator]` cached 1800 s alongside `assess_all` (same streamed pass; refactor `assess_all` to build both).

- [ ] Step 1: failing tests — `test_overconfident_long_shots_are_pulled_down` (300 rows at p=0.15 with 15 hits; `apply(0.15, 0.10)` == pytest.approx((300*0.05 + 30*0.10)/330)); `test_empty_bin_returns_market_price` (`apply(0.85, 0.80) == 0.80`); `test_no_market_price_anchors_to_raw` (empty → `apply(0.4, None) == 0.4`); `test_table_reports_each_populated_bucket`.
- [ ] Step 2–4: run → FAIL, implement, run → PASS + full suite.
- [ ] Step 5: commit `"Calibrated hit %: per-family binned calibration shrunk toward the market"`

### Task 3: Board assembly + `/api/board`

**Files:** Create `backend/app/research/board.py`; modify `backend/app/api/routes_trading.py` (extract `build_cards(analysis, user) -> tuple[list[Recommendation], list[Recommendation]]` from `opportunities()`, which then calls it; add `GET /api/board`). Test `backend/tests/test_board.py`.

**Interfaces:**
- Consumes: Task 1 `Evidence`, `family_key`; Task 2 `Calibrator.apply`; `Recommendation` fields (`recommendation_id, game_id, kickoff, matchup, ticker, market_type, selection, side, final_prob, market_prob, cost, estimated_fee, contracts, max_entry_price, robust, supporting, missing, warnings, recommended_stake, model_version`); `fee_model.trading_fee`.
- Produces: `to_board_card(card: Recommendation, *, evidence: Evidence | None, calibrator: Calibrator | None, live_allowed: bool) -> dict` with keys
  `id, venue ("kalshi"), ticker, series, matchup, kickoff, selection, side, market_type, price, hit, raw_hit, edge_cents, max_entry_price, robust, reasons, missing, warnings, evidence {state, calls, clusters, reason, model_log_loss, market_log_loss, roi_after_fees}, stake, trade {url, in_app}`.
  - Family for a board card is `("ensemble", card.market_type)`.
  - `hit = calibrator.apply(final_prob, market_prob)` (no calibrator → `final_prob` shrunk to market with n=0, i.e. market_prob if present).
  - `edge_cents = round((hit - price) * 100 - fee_per_contract * 100, 1)` where `fee_per_contract = trading_fee(contracts=max(card.contracts,1), price) / max(card.contracts,1)`.
  - `stake = card.recommended_stake if state == "PROVEN" else 0`.
  - `series` = ticker up to the first `-`; `trade.url = f"https://kalshi.com/markets/{series.lower()}"`.
- `GET /api/board` → `{"season","week","built_at","cards":[...] (deduped by ticker+side, sorted edge_cents desc), "counts": {"PROVEN":n,"WATCHING":n,"FAILING":n}, "empty_reason", "thresholds"}`; cards with `edge_cents <= 0` dropped.

- [ ] Step 1: failing tests — `test_watching_family_gets_zero_stake`; `test_hit_is_calibrated_and_edge_uses_it` (card final 0.30 at price 0.25 with a calibrator whose 0.3 bin hit 20% on 300 rows → hit < 0.30 and edge from calibrated hit); `test_unknown_family_is_watching`; `test_kalshi_link_from_series`; `test_board_endpoint_private`.
- [ ] Step 2–4. Step 5: commit `"Trade Board API: calibrated hit %, after-fee edge, evidence state per card"`

### Task 4: Kalshi + Polymarket US market browser

**Files:** Create `backend/app/data/kalshi/browse.py`, `backend/app/data/polymarket_us.py`, `backend/app/api/routes_markets.py` (router registered in `main.py`). Test `backend/tests/test_market_browse.py` (fixtures from real payload shapes, no network).

**Interfaces:**
- `@dataclass BrowseMarket: venue: Literal["kalshi","polymarket"]; id: str; title: str; category: str; yes_bid: float|None; yes_ask: float|None; last: float|None; volume: float|None; closes: str|None; url: str` + `mid` property.
- `kalshi.browse.parse_events(payload: dict) -> list[BrowseMarket]` (from `/events?status=open&with_nested_markets=true&limit=200`; uses `yes_bid_dollars`, `yes_ask_dollars`, `last_price_dollars`, `volume_fp`, event `category`, `close_time`; skips markets with both bid and ask absent/zero; url `kalshi.com/markets/<series_ticker lower>`).
- `polymarket_us.parse_markets(payload: dict) -> list[BrowseMarket]` (from `/v1/markets?active=true&closed=false&limit=200`; `question`, `category`, `endDate`, `slug`; YES price from the `marketSides` entry whose `description` is "Yes"; skip unparseable; url `polymarket.us/event/<slug>`).
- `async fetch_all() -> list[BrowseMarket]` both venues concurrently, 120 s cache, a venue that errors contributes `[]` and a warning.
- `GET /api/markets/browse?q=&category=` → `{"markets":[...], "categories":[...], "warnings":[...], "as_of"}` filtered case-insensitively on title.

- [ ] Step 1: failing tests — Kalshi parse yields bid/ask from the 2026-11-23 shape; skips empty book; Polymarket parse reads YES side price; skips market with no sides (Review Focus 4); `test_one_venue_down_still_returns_the_other` (monkeypatched fetchers).
- [ ] Step 2–4. Step 5: commit `"Markets browser: Kalshi open events and Polymarket US, side by side"`

### Task 5: Decision journal

**Files:** Modify `backend/app/tracking/store.py` (table `journal`: `id TEXT PK, created_at REAL, username TEXT, ticker TEXT, side TEXT, venue TEXT, price REAL, contracts INTEGER, card_json TEXT, note TEXT`); create `backend/app/research/journal.py`; endpoints in `routes_research.py`. Test `backend/tests/test_journal.py`.

**Interfaces:**
- `record(username, card: dict, *, price: float, contracts: int, note: str = "") -> str` (stores the card snapshot verbatim).
- `entries(username) -> list[dict]` each with `outcome`: latest settled prediction for the same ticker (+ selection) → `"won"|"lost"`, else `"open"`; plus `pnl` = contracts × (1 − price) if won, −contracts × price if lost.
- `summary(entries) -> dict` keyed by evidence state at time of trade: `{trades, won, lost, pnl}`.
- `POST /api/journal` body `{card, price, contracts, note}`; `GET /api/journal` → `{"entries", "summary"}`.

- [ ] Step 1: failing tests — round-trip keeps the card snapshot; outcome resolves from a settled prediction on the ticker; summary groups by the card's evidence state; other users' entries not returned.
- [ ] Step 2–4. Step 5: commit `"Decision journal: every trade from the board, its reasons, and how it did"`

### Task 6: Board page (frontend)

**Files:** Create `frontend/src/pages/Board.tsx`, `frontend/src/board/{TradeCard,Ticket,types}.tsx|ts`; route `board` in `sections.ts` → `module: null`, render Board. Test `frontend/src/board/board.test.tsx`.

**Interfaces:**
- Consumes `/api/board`, `/api/me` (`live_trading.allowed`), `POST /api/orders/preflight`, `POST /api/orders` (`{ticker, side, stake, mode, model_prob, recommended_cost, max_entry_price, recommendation_id, game_id, market_type, selection, label}`), `POST /api/journal`.
- Card layout per spec sketch: line 1 matchup · venue · kickoff; line 2 `HIT xx%` `PRICE xx¢` `EDGE +x.x¢ after fees` + evidence chip (`● PROVEN (n calls)` / `○ WATCHING` / `✕ FAILING`); line 3 `why:` reasons joined with ` · `; line 4 evidence reason + record; `[TRADE]`. Raw hit in `title` tooltip on the HIT figure.
- Filters: `Proven` (default) / `All`; sort `Edge` (default) / `Hit` / `Kickoff`. Empty proven list shows: "No proven trades right now. {n} ideas are being watched." with a button to show all.
- `[TRADE]`: `trade.in_app` → `Ticket` modal (contracts input, live preflight result, mode Live/Paper, confirm → `/api/orders` then `/api/journal`); else opens `trade.url` in a new tab and shows "Log this trade?" (price, contracts, note) → `/api/journal`.
- `?q=TEAM` (from Module 1 search) filters cards by matchup text.

- [ ] Step 1: failing tests — default filter hides WATCHING cards and shows the empty message with the watching count; `All` shows them; HIT shows calibrated value and the tooltip holds raw; hosted card opens link + log prompt (window.open stubbed) and POSTs `/api/journal`; in-app card runs preflight before enabling confirm; `?q=CIN` filters.
- [ ] Step 2–4 (`npm test`, `npm run build`). Step 5: commit `"Trade Board page"`

### Task 7: Research and Markets pages

**Files:** Create `frontend/src/pages/{Research,Markets}.tsx`, `frontend/src/panels/{EvidenceTable,Calibration,Journal,Briefs}.tsx`; `sections.ts` research + markets → built. Test `frontend/src/pages/research-markets.test.tsx`.

- Research: panels Evidence (one row per family: state chip, calls, games, model vs market log loss, 95% interval, ROI after fees, reason; thresholds line "PROVEN needs ≥150 calls, ≥40 games, interval below 0, ROI after fees > 0"), Calibration (family picker → table predicted vs observed per bucket with n), Journal (entries + summary by evidence state), Briefs (Task 8; shows "Briefs are off: no API key configured" when disabled).
- Markets: search box + category select; two columns Kalshi | Polymarket US; rows title, bid/ask (or price), volume, closes, venue link; venue warnings shown in the panel.

- [ ] Step 1: failing tests — evidence row shows WORSE/FAILING chip and reason text; thresholds line present; markets search filters both columns; a venue warning renders while the other column still lists markets.
- [ ] Step 2–4. Step 5: commit `"Research and Markets pages"`

### Task 8: Research briefs (Claude)

**Files:** Create `backend/app/research/briefs.py`; endpoint `GET /api/research/briefs` in `routes_research.py`; `anthropic` added to `backend/requirements.txt`. Test `backend/tests/test_briefs.py`.

**Interfaces:**
- Load the `claude-api` skill before writing this task (model id, SDK usage). Model: the current most capable Claude model per that skill.
- `build_prompt(cards: list[dict]) -> str`: data-only prompt — the top 3 board cards' JSON (matchup, hit, price, edge, reasons, missing, evidence) and instructions: write ≤ 120 words per card, every claim must cite a field from the data in `[brackets]`, say "insufficient data" instead of guessing, never recommend a stake.
- `async generate(cards) -> dict` → `{"enabled": True, "generated_at", "briefs":[{"card_id","text"}]}`; cached 6 h keyed by card ids; when `ANTHROPIC_API_KEY` unset → `{"enabled": False, "reason": "No ANTHROPIC_API_KEY configured."}`; on API error → last cache or `{"enabled": True, "briefs": [], "error": "<message>"}`.

- [ ] Step 1: failing tests — no key → enabled False; prompt contains every card's fields and the citation rule; API error path returns 200 with error and no exception (client stubbed); cache hit avoids a second call.
- [ ] Step 2–4. Step 5: commit `"Research briefs written from the terminal's own data, citations required"`

### Task 9: Demo + ship prep

- [ ] `scripts/snapshot_demo.py`: add `/board`, `/research/evidence`, `/markets/browse`, `/research/briefs` to `ENDPOINTS`; demo `/journal` = `{"entries":[],"summary":{}}`. Snapshot from local instance (prod is private after deploy; until then prod is public for the old endpoints only).
- [ ] Extend `test_demo_snapshot.py` forbidden-key check (already covers new files by glob). Board cards in demo: `trade.in_app` forced false.
- [ ] Full suites: backend pytest, frontend `npm test && npm run build`. Run locally, screenshot `/demo/board`, `/demo/research`, `/demo/markets`.
- [ ] Commit. **Do not merge to main** — ask Ashar (merge = deploy).
