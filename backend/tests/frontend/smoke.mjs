/* Frontend smoke test.
 *
 * Runs every view's real rendering code against a running server, under a DOM shim just
 * large enough to execute it. Static checks tell you a file parses; this tells you the
 * dashboard does not throw when the board is empty, that the game page survives a game
 * with no injuries, and that a view whose data comes back in an unexpected shape fails
 * loudly instead of rendering a blank panel.
 *
 * Usage:  node tests/frontend/smoke.mjs [base-url]
 * The server must be running. Exits non-zero if any view fails.
 */
import "./dom-shim.mjs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const BASE = process.argv[2] || process.env.ALPHA_BASE || "http://127.0.0.1:8000";
const here = dirname(fileURLToPath(import.meta.url));
const jsDir = resolve(here, "../../static/js");

const realFetch = globalThis.fetch;
let cookie = "";
globalThis.fetch = (path, opts = {}) => {
  const headers = { ...(opts.headers || {}) };
  if (cookie) headers.Cookie = cookie;
  return realFetch(path.startsWith("http") ? path : BASE + path, { ...opts, headers })
    .then((r) => {
      const set = r.headers.get("set-cookie");
      if (set) cookie = set.split(";")[0];
      return r;
    });
};

const research = await import(resolve(jsDir, "views-research.js"));
const model = await import(resolve(jsDir, "views-model.js"));
const portfolio = await import(resolve(jsDir, "views-portfolio.js"));

/** Is a loading skeleton still on screen anywhere in this subtree? */
function stillLoading(node) {
  if (node?.className?.includes?.("skeleton")) return true;
  for (const child of node?.childNodes || []) if (stillLoading(child)) return true;
  return false;
}

/** Wait until the mount stops changing AND no skeleton remains.
 *
 * Both conditions are needed: a view whose body is still a skeleton has stable text
 * content (skeletons render no text), so a size-only check returns while the real work
 * is still in flight — which is exactly how the backtest view slipped through as a pass
 * with 77 characters of chrome and nothing in it. */
async function settle(mount, maxMs = 90000) {
  let last = -1;
  let stableFor = 0;
  const deadline = Date.now() + maxMs;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 400));
    if (stillLoading(mount)) { stableFor = 0; continue; }
    const size = (mount.textContent || "").length;
    if (size === last && size > 0) {
      stableFor += 1;
      if (stableFor >= 2) return;
    } else {
      stableFor = 0;
    }
    last = size;
  }
}

async function run(name, fn, ctx = {}) {
  const mount = new globalThis.Node("div");
  try {
    await fn(mount, { navigate() {}, params: {}, session: { user: null }, ...ctx });
    await settle(mount);
    const text = mount.textContent || "";
    if (/This view failed|Could not load this view/.test(text)) {
      console.log(`  FAIL  ${name}: rendered an error state`);
      return false;
    }
    if (!mount.childNodes.length) {
      console.log(`  FAIL  ${name}: produced no output`);
      return false;
    }
    console.log(`  ok    ${name} (${mount.childNodes.length} nodes, ${text.length} chars)`);
    return true;
  } catch (err) {
    console.log(`  FAIL  ${name}: ${err.message}`);
    if (process.env.VERBOSE) console.log(err.stack);
    return false;
  }
}

const health = await (await fetch("/api/health")).json();
const slate = await (await fetch("/api/slate")).json();
const gameId = slate.games?.[0]?.game?.game_id;
console.log(`Server: ${BASE} — season ${health.season} week ${health.week}, `
  + `model ${health.model.calibrated ? "calibrated" : "calibrating"}\n`);

// Sign in so the account-gated branches are exercised too, not just their empty states.
await fetch("/api/signup", {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ username: "smoketest", password: "smoketest-pw" }),
}).catch(() => {});
await fetch("/api/login", {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ username: "smoketest", password: "smoketest-pw" }),
}).catch(() => {});

const results = [];
results.push(await run("dashboard", research.dashboard));
results.push(await run("games", research.games));
results.push(await run("predictions", research.predictions));
results.push(await run("markets", research.markets));
results.push(await run("parlays", portfolio.parlays));
results.push(await run("strategies", model.strategies));
results.push(await run("model lab", model.modelLab));
results.push(await run("performance", model.performance));
results.push(await run("backtests", model.backtests));
results.push(await run("portfolio (signed in)", portfolio.portfolio,
  { session: { user: "smoketest" } }));
results.push(await run("settings (signed in)", portfolio.settings,
  { session: { user: "smoketest" } }));
if (gameId) results.push(await run(`game detail (${gameId})`, research.gameDetail,
  { params: { gameId } }));

// Prediction cards only appear when the board has value bets, which most slates do not
// have. Render one directly so the card path is covered on every run regardless.
results.push(await run("prediction card (synthetic)", async (mount) => {
  mount.append(research.predictionCard({
    strategy: "ensemble", game_id: "2026_01_NE_SEA", market_type: "spread",
    label: "Seahawks by more than 3.5", selection: "Seahawks -3.5",
    model_prob: 0.58, market_prob: 0.53, edge: 0.05, ev_per_dollar: 0.09,
    confidence: "high", line: 3.5, team: "SEA", actionable: true,
    reasoning: ["Projected margin 4.2 to Seattle", "P(win by more than 3.5) = 58%"],
    quote: { ticker: "KXNFLSPREAD-TEST", depth_usd: 4200 },
    features: { fair_prob: 0.58, cost: 0.53, decimal_odds: 1.89, depth_usd: 4200,
                liquid: true, value: true, raw_model_prob: 0.62 },
  }, () => {}));
}));

const failed = results.filter((r) => !r).length;
console.log(failed ? `\n${failed} view(s) failed` : `\nall ${results.length} views rendered`);
process.exit(failed ? 1 : 0);
