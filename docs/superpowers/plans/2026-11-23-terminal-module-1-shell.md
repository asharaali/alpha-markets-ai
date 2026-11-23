# Terminal Module 1 — Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A private, login-gated React terminal shell (layout, ticker, nav, search, panel framework, demo mode) served by the existing FastAPI app, with the first NFL panels wired to existing endpoints.

**Architecture:** New `frontend/` (Vite + React + TS) builds into `backend/static/terminal/`, committed, so Render's Python build needs no Node. FastAPI serves it at `/` and `/demo`; the old vanilla SPA moves to `/classic`. A middleware makes every `/api/*` route require a session except an explicit allowlist.

**Tech Stack:** Python 3.12 / FastAPI / pytest (existing); Node 22, Vite 6, React 18, TypeScript 5, Vitest + Testing Library, React Router 6. No UI kit.

**Spec:** `docs/superpowers/specs/2026-11-23-terminal-design.md`

## Global Constraints

- Private: every `/api/*` route requires login except `/api/health`, `/api/login`, `/api/logout`, `/api/me`.
- Live trading stays disabled on the hosted instance; no Kalshi key on Render.
- Every panel shows title, status chip (LIVE / LOADING / STALE / ERROR / DEMO), as-of time, and source. Stale is visible, never silent.
- Demo mode shows no balances, positions, account ids, usernames or bankroll.
- Own identity: "Alpha Markets Terminal". No Sorcerer name, logo, copy or exact styling.
- Shell HTML served with `Cache-Control: no-cache` (existing rule).
- Nothing > 0.5 s CPU on the event loop.

## Review Focus

1. Session expires while the terminal is open → API returns 401 → app drops to the login screen instead of showing a wall of ERROR panels. (Task 4 test.)
2. `/demo` opened by a logged-in user → still shows demo data, never live data, and the demo banner. (Task 4 test.)
3. An endpoint returns data whose `as_of` is older than the panel's `maxAgeSec` → STALE chip, data still shown. (Task 3 test.)
4. A new `/api/*` route added later without thinking → private by default. (Task 1 test enumerates all routes.)
5. Built assets missing on a fresh clone → `/` returns a clear 503 page naming the build command, not a 500. (Task 2 test.)

---

### Task 1: Private-by-default API

**Files:**
- Create: `backend/app/api/privacy.py`
- Modify: `backend/app/main.py` (register middleware after app creation)
- Test: `backend/tests/test_private_api.py`

**Interfaces:**
- Produces: `PUBLIC_API_PATHS: frozenset[str]` = `{"/api/health", "/api/login", "/api/logout", "/api/me"}`; `class PrivateApiMiddleware` (Starlette `BaseHTTPMiddleware`) returning `401 {"error":"unauthorized","message":"Log in to use the terminal."}` for any other `/api/*` without a valid session (`deps.current_user(request)`).

- [ ] **Step 1: Write failing tests**
  - `test_every_api_route_is_private_unless_allowlisted`: iterate `app.routes`, for each GET path under `/api/` not in `PUBLIC_API_PATHS` (path params filled with `x`), unauthenticated `TestClient.get` → status 401.
  - `test_allowlisted_routes_stay_public`: `/api/health` → 200; `/api/me` → 200 with `{"user": None}`.
  - `test_logged_in_user_reaches_private_routes`: create user via `auth.create_user` in a tmp DATA_DIR (reuse `conftest` fixtures), login, `GET /api/strategies` → 200.
  - `test_non_api_paths_unaffected`: `GET /` → not 401.
- [ ] **Step 2: Run** `cd backend && .venv/bin/python -m pytest tests/test_private_api.py -v` → FAIL (strategies returns 200 unauthenticated).
- [ ] **Step 3: Implement** `privacy.py` and register in `main.py`.
- [ ] **Step 4: Run** the new file → PASS; then full suite `.venv/bin/python -m pytest -q` → all pass (fix any existing test that called a now-private endpoint unauthenticated by logging in via fixture, not by widening the allowlist).
- [ ] **Step 5: Commit** `git commit -m "Make every API route private unless explicitly allowlisted"`

### Task 2: Serve terminal at `/` and `/demo`, old app at `/classic`

**Files:**
- Modify: `backend/app/main.py` (routes `index`, `spa`)
- Move: `backend/static/index.html` → `backend/static/classic/index.html` (update its `/static/...` asset paths only if they break; they are absolute so they should not)
- Test: `backend/tests/test_shell_caching.py` (extend)

**Interfaces:**
- Produces: `TERMINAL_DIR = STATIC_DIR / "terminal"`; `/`, `/demo`, and any non-API, non-file path → `TERMINAL_DIR/index.html` with `_SHELL_HEADERS`; `/classic` and `/classic/{path}` → `static/classic/index.html` with `_SHELL_HEADERS`; when `TERMINAL_DIR/index.html` is missing → 503 HTML containing `cd frontend && npm run build`.

- [ ] **Step 1: Failing tests:** `test_terminal_served_at_root_and_demo` (create a temp `terminal/index.html` marker via monkeypatched `TERMINAL_DIR`; `/`, `/demo`, `/board` return it, no-cache); `test_classic_still_served`; `test_missing_build_is_503_with_instructions`; existing traversal tests still pass.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** full suite → PASS.
- [ ] **Step 5: Commit** `"Serve the terminal at / and /demo; old app moves to /classic"`

### Task 3: Frontend scaffold, API client, panel framework

**Files:**
- Create: `frontend/package.json`, `frontend/vite.config.ts` (`base: "/static/terminal/"`, `build.outDir: "../backend/static/terminal"`, `emptyOutDir: true`), `frontend/tsconfig.json`, `frontend/index.html`, `frontend/src/main.tsx`
- Create: `frontend/src/lib/api.ts`, `frontend/src/lib/freshness.ts`, `frontend/src/components/Panel.tsx`, `frontend/src/styles/terminal.css`
- Test: `frontend/src/lib/freshness.test.ts`, `frontend/src/lib/api.test.ts`, `frontend/src/components/Panel.test.tsx`

**Interfaces:**
- Produces:
  - `type PanelStatus = "LIVE" | "LOADING" | "STALE" | "ERROR" | "DEMO"`
  - `freshness(asOf: number | null, maxAgeSec: number, now?: number): "LIVE" | "STALE"` (asOf in epoch seconds; null → STALE)
  - `class UnauthorizedError extends Error`
  - `isDemo(): boolean` (true when `location.pathname` starts with `/demo`)
  - `apiGet<T>(path: string): Promise<T>` — live: `fetch("/api"+path, {credentials:"include"})`, 401 → throws `UnauthorizedError`; demo: fetches `/static/terminal/demo` + path-with-slashes-to-underscores + `.json` (e.g. `/slate` → `demo/slate.json`).
  - `useApi<T>(path: string, opts: {maxAgeSec: number, asOf: (d: T) => number | null, refreshSec?: number}): {data?: T, status: PanelStatus, asOf: number | null, error?: string}`
  - `<Panel title source status asOf error? actions? children>`

- [ ] **Step 1:** `npm create vite@latest` equivalent by hand; deps: react, react-dom, react-router-dom; dev: vite, @vitejs/plugin-react, typescript, vitest, jsdom, @testing-library/react, @testing-library/jest-dom. Scripts: `dev` (proxy `/api` → `http://127.0.0.1:8000`), `build` (`tsc -b && vite build`), `test` (`vitest run`).
- [ ] **Step 2: Failing tests:** freshness — 30 s old with max 60 → LIVE; 61 s → STALE; null → STALE. api — 401 throws `UnauthorizedError`; demo path maps `/strategies` to `/static/terminal/demo/strategies.json`. Panel — renders title, source, "STALE" chip text, and as-of as local `HH:MM`; ERROR shows the error message.
- [ ] **Step 3: Run** `cd frontend && npm test` → FAIL.
- [ ] **Step 4: Implement.** Styles: dark background, dense 1px-bordered panels, monospace numerals, one accent colour — our own palette.
- [ ] **Step 5: Run** `npm test` → PASS; `npm run build` → writes `backend/static/terminal/index.html`.
- [ ] **Step 6: Commit** frontend sources + built output: `"Terminal frontend scaffold: API client, freshness, Panel"`

### Task 4: App shell — auth gate, nav, ticker, search, demo banner

**Files:**
- Create: `frontend/src/App.tsx`, `frontend/src/shell/{TopBar,Ticker,Nav,Search,LoginScreen,DemoBanner}.tsx`, `frontend/src/auth.ts`, `frontend/src/pages/{Home,ComingSoon}.tsx`
- Test: `frontend/src/App.test.tsx`

**Interfaces:**
- Consumes: Task 3 `apiGet`, `UnauthorizedError`, `isDemo`, `Panel`.
- Produces: routes `/` (Home), `/board`, `/markets`, `/sports`, `/macro`, `/politics`, `/pnl`, `/research` (all but Home render `ComingSoon` naming its module number from the spec), each mirrored under `/demo/*`. `useSession(): {user: string | null, loading: boolean, login(u,p), logout()}` over `/api/me`, `/api/login`, `/api/logout`. A global listener: any `UnauthorizedError` from `useApi` → session cleared → LoginScreen.
- Ticker items from existing data only in this module (NFL: today's games and model-vs-market headline); macro tickers arrive in Module 4.
- Search: client-side over loaded slate teams and nav pages; Enter navigates.

- [ ] **Step 1: Failing tests** (fetch mocked): logged out → LoginScreen; login success → Home; a 401 from any panel while logged in → back to LoginScreen; path `/demo` with a logged-in session → DemoBanner visible and requests go to `demo/*.json`, never `/api/*`; nav shows all 8 entries.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `npm test` → PASS, `npm run build`.
- [ ] **Step 5: Commit** `"Terminal shell: login gate, nav, ticker, search, demo mode"`

### Task 5: Home panels on existing NFL data

**Files:**
- Create: `frontend/src/panels/{GameOfTheDay,NflSlate,ModelVsMarket,JobsHealth}.tsx`
- Test: `frontend/src/panels/panels.test.tsx`

**Interfaces:**
- Consumes: existing endpoints `/api/slate` (`built_at`), `/api/strategies` (per-strategy `record.brier`, `brier_market`, `log_loss`, `log_loss_market`, `n`, `games`), `/api/status/jobs` (`last_run` per job), `/api/health`.
- `ModelVsMarket`: one row per strategy with `meaningful: true`: n, games, log loss vs market, and a verdict chip `BEATS MARKET` / `WORSE THAN MARKET` (log_loss < log_loss_market). This is the honest-record panel; no green unless it actually beats the market.
- `GameOfTheDay`: next unplayed game in slate with the smallest |model margin| (most competitive); shows teams, kickoff, model win %.
- `JobsHealth`: each job's last run age; STALE if older than 3× its interval (interval from `/api/status/jobs` if present, else 3600 s).

- [ ] **Step 1: Failing tests** with fixtures copied from today's live shapes: ModelVsMarket marks moneyline (0.3191 vs 0.2866) `WORSE THAN MARKET`; GameOfTheDay picks the unplayed game, skips completed ones; JobsHealth STALE when last_run is 4 h old with 30-min interval.
- [ ] **Step 2–4:** run → FAIL, implement, run → PASS, build.
- [ ] **Step 5: Commit** `"Home panels: game of the day, slate, model vs market, job health"`

### Task 6: Demo snapshot

**Files:**
- Create: `backend/bin/snapshot_demo.py`, output `frontend/public/demo/*.json`
- Test: `backend/tests/test_demo_snapshot.py`

**Interfaces:**
- `snapshot_demo.py --base URL --user U` (password from `ALPHA_PW` env or prompt; never argv) fetches `/slate`, `/strategies`, `/status/jobs`, `/health`, `/me`, writes JSON, and passes each through `sanitize(obj) -> obj`, which drops keys matching `(?i)balance|bankroll|position|account|user|username|email|order_id|client_order|kalshi_key` at any depth and sets `/me` to `{"user": "demo", "live_trading": {"allowed": false, "reason": "demo"}}`.

- [ ] **Step 1: Failing test:** `sanitize` removes nested `bankroll`, `account_id`, `positions`; leaves `brier`, `games`. A second test loads every committed `frontend/public/demo/*.json` and asserts none of those keys appear.
- [ ] **Step 2–4:** run → FAIL, implement, generate snapshot against prod with Ashar's login (or local instance if he prefers), run → PASS, `npm run build`.
- [ ] **Step 5: Commit** `"Demo mode snapshot (sanitized)"`

### Task 7: Ship

- [ ] Full checks: `cd backend && .venv/bin/python -m pytest -q` (all pass), `cd frontend && npm test && npm run build`.
- [ ] Run locally: `./run.sh`, open `/`, `/demo`, `/classic`; screenshot each; log in and confirm panels LIVE.
- [ ] Push branch `terminal`; merging to `main` auto-deploys `alpha-markets` on Render — **ask Ashar before merging**. After deploy: `curl /api/strategies` unauthenticated → 401; `/api/health` → 200; `/demo` renders.
