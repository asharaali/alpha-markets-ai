# Terminal Module 4 — Macro & Rates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A Macro page (and Home tiles) with Treasury yields, Fed decision odds, inflation/jobs/GDP prints, recession odds, live markets and a fear gauge — all from free, keyless sources, honest about what is a print, a market price, or a quote.

**Architecture:** `backend/app/data/macro/` — one small client per source (FRED CSV, Kalshi macro series, Yahoo chart quotes, Coinbase spot, alternative.me F&G), each returning plain dataclasses with an `as_of`. A scheduler job `macro_refresh` (every 15 min; quotes every 2 min while US markets are open) writes one cached JSON blob; `GET /api/macro` only reads it (page loads never fetch upstream — the Module 2 lesson). Frontend: `pages/Macro.tsx` + panels; two Home tiles.

**Spec:** `docs/superpowers/specs/2026-11-23-terminal-design.md` (Module 4). Module 2 lessons: `docs/superpowers/plans/2026-11-23-terminal-module-2-ledger.md`.

## Global Constraints

- No API keys, no paid sources. CNN Fear & Greed is not used (it blocks automated clients; we do not work around bot detection).
- Every number carries `kind`: `print` (official release, e.g. CPI), `market` (a traded price, e.g. Kalshi Fed odds), `quote` (live/delayed price) — shown on the panel so a market-implied odd is never presented as a forecast of ours.
- Every panel: source + as-of; stale visibly marked (Module 1 Panel). Yahoo is unofficial → labeled "Yahoo (unofficial, delayed)"; FRED daily close is the fallback.
- `/api/macro` is private (route-walker test) and never performs upstream I/O on the request path.
- Nothing > 0.5 s CPU on the event loop.

## Review Focus

1. One source down (FRED 5xx, Yahoo 429) → its panel shows ERROR with the reason, every other panel still LIVE. (Task 3 test.)
2. FRED CSV with "." for a missing day (it does this on holidays) → skipped, latest real value used. (Task 1 test.)
3. Kalshi Fed event with several meeting dates → the NEXT meeting is shown, ordered by date, outcomes sorted by probability. (Task 2 test.)
4. Cold boot with no cached blob yet → `/api/macro` returns `{"ready": false}` quickly, page shows LOADING, not 500. (Task 3 test.)
5. Weekend / market closed → quotes keep last close and say "closed", not STALE-red. (Task 1 test.)

---

### Task 1: Source clients (FRED, Yahoo, Coinbase, alternative.me)

**Files:** `backend/app/data/macro/{__init__,fred,quotes,sentiment}.py`; fixtures `backend/tests/fixtures/macro/*`; test `backend/tests/test_macro_sources.py`.

- `fred.parse_csv(text) -> list[(date, float)]` (skip "."), `fred.SERIES` = DGS3MO, DGS2, DGS5, DGS10, DGS30, DFEDTARU, CPIAUCSL, CPILFESL, PCEPI, PCEPILFE, PPIFIS, UNRATE, A191RL1Q225SBEA, SP500, DCOILWTICO; `fred.indicator(series, rows) -> Indicator(id, label, value, prev, change, yoy|None, as_of, kind)` — YoY for index series (CPI/PCE/PPI), MoM % too; level for rates/unemployment/GDP growth.
- `quotes.parse_yahoo(payload) -> Quote(symbol, price, prev_close, change_pct, as_of, market_open: bool)`; symbols SPY, QQQ, GLD, USO, TLT, ^VIX; `quotes.parse_coinbase(payload)` BTC.
- `sentiment.parse_alternative(payload) -> Sentiment(value 0-100, label, as_of)`.
- HTTP via `make_client`; User-Agent must be accepted by FRED (test: the client UA string does not contain the token FRED rejected — verify live once).
- Tests: CPI YoY from fixture; "." rows skipped; Yahoo closed-market flag; Coinbase parse; alternative.me parse.

### Task 2: Kalshi macro markets

**Files:** `backend/app/data/macro/kalshi_macro.py`; test `backend/tests/test_macro_kalshi.py` (fixtures from live KXFED / KXRECSSNBER payloads).

- `fed_decision(payload) -> FedOdds(meeting_date, outcomes=[{label, prob, bid, ask}], url, as_of)` — next meeting only, outcomes by prob desc, prob = mid of yes bid/ask.
- `recession(payload) -> Odds(label, prob, url, as_of)`.
- Tests: next-meeting selection across multiple events; sorting; missing book skipped.

### Task 3: Refresh job + `/api/macro`

**Files:** `backend/app/data/macro/refresh.py`; `scheduler.job_specs()` gains `("macro_refresh", 900, ...)`; route in new `backend/app/api/routes_macro.py`; test `backend/tests/test_macro_api.py`.

- `async refresh() -> dict` gathers all sources with `return_exceptions=True`; blob `{"ready": True, "built_at", "sections": {"rates", "fed", "inflation", "jobs_growth", "recession", "markets", "sentiment"}, "errors": {section: message}}` written atomically to `CACHE_DIR/macro.json`.
- `GET /api/macro` reads the file; missing → `{"ready": false}`; never fetches.
- Tests: one source raising → that section in `errors`, others present; endpoint private; endpoint does no network (monkeypatch client to raise); missing file → ready false.

### Task 4: Macro page + Home tiles

**Files:** `frontend/src/pages/Macro.tsx`, `frontend/src/panels/macro/{Rates,FedOdds,Inflation,JobsGrowth,Recession,Markets,Sentiment}.tsx`, Home gets `MacroStrip` (10Y, Fed next-meeting top outcome, CPI YoY, recession odds, SPY); `sections.ts` macro → built; test `frontend/src/pages/macro.test.tsx`.

- Each value shows its `kind` chip (PRINT / MARKET / QUOTE). Section errors render in that panel only.
- Tests: kind chips render; a section error shows in its panel while others render; `ready: false` → LOADING.

### Task 5: Demo + ship

- Add `/macro` to `snapshot_demo.ENDPOINTS`; regenerate demo; full suites; local run + screenshots; deploy (Ashar approved deploys of finished modules — confirm in chat before merge); watch prod 6 min for any non-200.
