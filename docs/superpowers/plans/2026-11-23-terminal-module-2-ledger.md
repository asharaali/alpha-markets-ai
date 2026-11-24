# SDD ledger — plan: /Users/asharali/alpha-markets-ai/docs/superpowers/plans/2026-11-23-terminal-module-2-board.md
Pre-flight: T1 Evidence/family_key → T3 consumes (consistent); T2 Calibrator.apply → T3 (consistent); T3 /api/board card shape → T6 consumes; T4 BrowseMarket → T7 Markets; T5 /api/journal → T6,T7; T8 → T7 Briefs panel.
Task 1: Ruling: FAILING is checked before the sample-size gates — a significantly-worse family is hidden even on few games (book_consensus/total on local data) — cost if wrong: a family hidden on early bad luck; it un-hides when the interval crosses 0.
Task 1: Ruling: synthetic fixtures strengthened (skill 1.0 vs 0.3, p∈[0.05,0.95], 5/game; ev from model vs market) — 1.0 vs 0.6 over 180 calls is genuinely indistinguishable; verdicts stable over 5 seeds — cost: none.
Task 1: Ruling: ROI fee uses fees.fee_per_contract (unrounded) — per-$1 flat stake, rounding noise misleads — cost: tiny optimism vs order-level rounding.
Task 1: complete (commits c4512a6..4b963b0, tests: bash -c 'cd backend && .venv/bin/python -m pytest -q' → 426 passed, 1 warning in 8.48s)
Task 2: Ruling: calibrators() does its own grouped scan instead of sharing assess_all's pass — each scan < 1 s on 160k rows, two caches simpler — cost: one extra DB scan per 30 min.
Task 2: complete (commits 4b963b0..d63c029, tests: bash -c 'cd backend && .venv/bin/python -m pytest -q' → 431 passed, 1 warning in 8.32s)
Task 3: Ruling: board merges value cards AND likely-winner cards (deduped ticker+side, best edge kept), drops edge ≤ 0 at the calibrated hit — spec says the board lists recommended trades; both lists are candidates — cost: none.
Task 3: Ruling: unknown family reason text "No settled history for this kind of bet yet." — cost: none.
Task 3: Ruling: added test_build_cards_runs… after the extraction dropped the `rec` import (caught RED→GREEN) — cost: none.
Task 3: complete (commits d63c029..cfa95d1, tests: bash -c 'cd backend && .venv/bin/python -m pytest -q' → 439 passed, 1 warning in 8.96s)
Task 4: Ruling: BrowseMarket lives in app/data/browse_market.py (shared by both venues), fetch_all/cache in routes_markets — cost: none.
Task 4: Ruling: Polymarket US prices from bestBidQuote/bestAskQuote, YES-side price as last — outcomePrices ordering contradicted the YES side in the live payload — cost if wrong: inverted prices; verify against polymarket.us UI once.
Task 4: Ruling: Polymarket volume = None (no volume field in the gateway list) — cost: volume column blank for Polymarket.
Task 4: complete (commits cfa95d1..d470d30, tests: bash -c 'cd backend && .venv/bin/python -m pytest -q' → 444 passed, 1 warning in 8.71s)
Task 5: Ruling: NO-side trades win when the YES outcome is 0 (plan only spelled YES) — predictions store YES outcomes — cost: none.
Task 5: Ruling: summary also counts "open" per state (plan listed trades/won/lost/pnl) — open trades would otherwise vanish from the count — cost: none.
Task 5: complete (commits d470d30..674a4ac, tests: bash -c 'cd backend && .venv/bin/python -m pytest -q' → 450 passed, 1 warning in 8.45s)
Task 6: Ruling: ?q filter matches matchup, ticker, game_id and selection — Module 1 search sends team abbreviations (CIN) which only appear in ticker/game_id — cost: none.
Task 6: Ruling: in-app ticket defaults to PAPER mode, contracts capped at 50 by default — safer default for a one-click flow; Live is one select away — cost: an extra click for live orders.
Task 6: Ruling: hosted [TRADE] opens the venue AND shows the log-trade dialog (Skip available) — journal only works if trades get logged — cost: one dismiss click.
Task 6: complete (commits 674a4ac..652ada7, tests: bash -c 'cd frontend && npx vitest run' →    Duration  2.59s (environment 69%, setup 15%, tests 8%, import 5%, transform 3%))
Task 7: Ruling: calibration tables ride on /api/research/evidence (each family gets "calibration") instead of a separate endpoint — the plan had no endpoint for the Calibration panel; one fetch feeds both panels — cost: none.
Task 7: Ruling: Markets columns cap at 150 rows each (1,945 live markets) — filter/search narrows — cost: long tail only reachable via search.
Task 7: complete (commits 652ada7..c916fd0, tests: bash -c 'cd backend && .venv/bin/python -m pytest -q 2>&1 | tail -1 && cd ../frontend && npx vitest run 2>&1 | grep 'Tests '' →       Tests  30 passed (30))
Task 8: Ruling: one call per card (plain text, ≤120 words) instead of one call returning JSON for all — nothing to parse, one bad card can't sink the rest — cost: 3 requests instead of 1.
Task 8: Ruling: claude-opus-5-5, effort "medium" set explicitly, server-side refusal fallback ON (fallbacks="default", beta server-side-fallback-2026-07-01) per the claude-api skill default; a surviving refusal is reported as an error, not shown — cost: none.
Task 8: Ruling: board_payload() extracted so /api/research/briefs reuses the board; board failure → {"enabled": false, reason} not 500 — cost: none.
Task 8: NOT verified against the live API — no ANTHROPIC_API_KEY on this machine; SDK 2.13 signature accepts fallbacks/output_config/betas.
Task 8: complete (commits c916fd0..a715cd9, tests: bash -c 'cd backend && .venv/bin/python -m pytest -q' → 457 passed in 9.51s)
Task 9: Ruling: snapshot generated in-process from this checkout with a throwaway local account (random password, disabled after) — prod lacks the new endpoints and no password needed — cost: one disabled user row in the local DB.
Task 9: Ruling: demo status_jobs.json kept from the prod snapshot (local run had jobs disabled → empty); markets trimmed to 250 Kalshi + 150 Polymarket (596 KB → 120 KB) — cost: demo shows a subset.
Final: fixed Kalshi multi-outcome rows all titled "Who will the next Pope be?" — test_kalshi_multi_outcome_rows_name_the_outcome RED→GREEN, suite 460/460
Final: Ruling: hosted [TRADE] never opens the paper ticket (in_app follows live_trading_available, false on Render) — spec line "paper orders remain available in-app" holds only on the local instance — cost: no paper trading from the hosted site.
Final review: self-review (no subagent spawned — session policy); Review Focus 1-5 each covered by a named test.
Task 9: complete (commits a715cd9..f3ad1a2, tests: bash -c 'cd backend && .venv/bin/python -m pytest -q 2>&1 | tail -1' → 460 passed in 9.46s)
