# SDD ledger — plan: /Users/asharali/alpha-markets-ai/docs/superpowers/plans/2026-11-23-terminal-module-1-shell.md
Pre-flight: T3 produces apiGet/UnauthorizedError/isDemo/Panel → T4,T5 consume (consistent). T4 demo path mapping (demo/<path>.json) ↔ T6 writes frontend/public/demo/*.json → vite copies public/ to outDir so served at /static/terminal/demo/ (consistent).
Task 1: Ruling: a gate middleware already existed (denylist-by-public-prefix) — replaced its lists with privacy.PUBLIC_API_PATHS instead of adding a second middleware — cost if wrong: none, one gate is simpler.
Task 1: Ruling: route walker uses app.openapi() paths (FastAPI nests included routers as _IncludedRouter) — mutation-checked: leaking /api/slate is caught — cost if wrong: an include_in_schema=False API route would escape the walker (none exist).
Task 1: Ruling: test_signup_is_closed_by_default now accepts 401|403 — the private gate refuses before the closed-signup check; user still not created — cost: none.
Task 1: complete (commits 7cace87..cedea5e, tests: bash -c 'cd backend && .venv/bin/python -m pytest -q' → 412 passed, 1 warning in 7.47s)
Task 2: Ruling: existing test_shell_is_revalidated_on_every_load now uses the terminal_build fixture — / serves the React build, which does not exist until Task 3 — cost: none.
Task 2: complete (commits cedea5e..bb7fd27, tests: bash -c 'cd backend && .venv/bin/python -m pytest -q' → 415 passed, 1 warning in 7.48s)
Task 3: Ruling: current majors instead of plan's pins (Vite 8, React 19, react-router 8, TS 7, Vitest 5) — @vitejs/plugin-react needs Vite 8, react-router 6 had 2 moderate advisories, router 8 needs React 19; npm audit 0 vulns — cost if wrong: API churn only; package is "react-router" not "react-router-dom".
Task 3: Ruling: apiGet also dispatches window event am:unauthorized on 401 (Task 4's global listener hooks it) — one place to detect session expiry — cost: none.
Task 3: complete (commits bb7fd27..8730168, tests: bash -c 'cd frontend && npx vitest run' →    Duration  590ms (environment 77%, setup 18%, tests 2%, transform 2%, import 1%))
Task 4: Ruling: auth lives in src/auth.tsx (JSX provider) not auth.ts; sections table in src/sections.ts maps nav → spec module (Board/Markets/Research=2, P&L=3, Macro=4, Sports=5, Politics=6) — cost: none.
Task 4: Ruling: demo mode never calls /api/me — session is the fixed user "demo" — so demo cannot leak live data even with a live cookie — cost: none.
Task 4: Ruling: search team hits route to /board?q=TEAM (Board is Module 2; query param inert until then) — cost: dead-end hit until Module 2.
Task 4: complete (commits 8730168..1b29286, tests: bash -c 'cd frontend && npx vitest run' →    Duration  639ms (environment 67%, setup 16%, tests 11%, import 4%, transform 2%))
Task 5: Ruling: job intervals hard-coded from render.yaml (market_snapshots 600, grading 1800, else 3600) — /api/status/jobs reports none — cost if wrong: STALE threshold off if env intervals change.
Task 5: Ruling: ModelVsMarket as-of = fetch time — /api/strategies payload has no timestamp — cost: chip says LIVE even if grading stalled (JobsHealth panel shows that instead).
Task 5: complete (commits 1b29286..9213493, tests: bash -c 'cd frontend && npx vitest run' →    Duration  864ms (environment 69%, setup 16%, tests 9%, import 3%, transform 2%))
Task 6: Ruling: script lives in backend/scripts/ not backend/bin/ — root .gitignore ignores bin/ (first task-done recorded before the commit failed; line removed and re-run) — cost: none.
Task 6: complete (commits 9213493..6f3eea0, tests: bash -c 'cd backend && .venv/bin/python -m pytest -q 2>&1 | tail -1 && cd ../frontend && npx vitest run 2>&1 | grep 'Tests '' →       Tests  21 passed (21))
Final: fixed demo JobsHealth reads STALE as snapshot ages — 'measures job age against the snapshot' RED→GREEN, suite 22/22 + backend green
Final review: self-review (no subagent spawned — session policy) against Review Focus 1-5: all covered by tests; extra finding fixed above.
