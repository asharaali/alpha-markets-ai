/* Application shell: routing, session, and the persistent chrome. */

import { api, el, relativeTime, pct } from "./core.js?v=2.1.4";
import { dashboard, games, gameDetail, predictions, markets } from "./views-research.js?v=2.1.4";
import { strategies, modelLab, performance, backtests } from "./views-model.js?v=2.1.4";
import { parlays, portfolio, settings, applyTheme } from "./views-portfolio.js?v=2.1.4";

const ROUTES = [
  { path: "#/", title: "Dashboard", group: "Overview", view: dashboard, nav: true },
  { path: "#/games", title: "Today's Games", group: "NFL", view: games, nav: true },
  { path: "#/predictions", title: "Predictions", group: "NFL", view: predictions, nav: true },
  { path: "#/parlays", title: "Parlays", group: "NFL", view: parlays, nav: true },
  { path: "#/markets", title: "Markets", group: "NFL", view: markets, nav: true },
  { path: "#/strategies", title: "Strategies", group: "Research", view: strategies, nav: true },
  { path: "#/model", title: "Model Lab", group: "Research", view: modelLab, nav: true },
  { path: "#/performance", title: "Performance", group: "Research", view: performance, nav: true },
  { path: "#/backtests", title: "Backtests", group: "Research", view: backtests, nav: true },
  { path: "#/portfolio", title: "Portfolio", group: "Account", view: portfolio, nav: true },
  { path: "#/settings", title: "Settings", group: "Account", view: settings, nav: true },
  { path: "#/game/", title: "Game", group: null, view: gameDetail, nav: false },
];

const session = { user: null, health: null };

function navigate(hash) {
  if (location.hash === hash) render();
  else location.hash = hash;
}

function matchRoute(hash) {
  if (hash.startsWith("#/game/")) {
    return { route: ROUTES.find((r) => r.path === "#/game/"),
             params: { gameId: decodeURIComponent(hash.slice("#/game/".length)) } };
  }
  const route = ROUTES.find((r) => r.path === hash) || ROUTES[0];
  return { route, params: {} };
}

function buildNav() {
  const nav = document.getElementById("nav");
  nav.replaceChildren();
  let currentGroup = null;
  for (const route of ROUTES.filter((r) => r.nav)) {
    if (route.group !== currentGroup) {
      currentGroup = route.group;
      nav.append(el("div", { class: "nav-group-label", text: currentGroup }));
    }
    nav.append(el("a", {
      href: route.path,
      class: location.hash === route.path ? "active" : "",
      dataset: { path: route.path },
    }, route.title));
  }
}

function highlightNav() {
  const hash = location.hash || "#/";
  for (const link of document.querySelectorAll("#nav a")) {
    link.classList.toggle("active", link.dataset.path === hash);
  }
}

async function render() {
  const hash = location.hash || "#/";
  const { route, params } = matchRoute(hash);
  document.getElementById("pageTitle").textContent = route.title;
  highlightNav();
  const mount = document.getElementById("view");
  mount.replaceChildren();
  try {
    await route.view(mount, { navigate, params, session });
  } catch (err) {
    mount.replaceChildren(el("div", { class: "state error" },
      el("h3", { text: "This view failed to render" }),
      el("p", { text: err?.message || String(err) })));
    // Surfaced rather than swallowed: a silent blank panel is the worst failure mode.
    console.error("view error", err);
  }
  window.scrollTo({ top: 0 });
}

async function refreshStatus() {
  const foot = document.getElementById("footStatus");
  const meta = document.getElementById("topMeta");
  try {
    const health = await api("/api/health");
    session.health = health;
    const calibrating = !health.model.calibrated;
    foot.replaceChildren(
      el("span", { class: `status-dot ${calibrating ? "warn" : "ok"}` }),
      calibrating ? "calibrating model…" : `season ${health.season} · week ${health.week ?? "—"}`);
    meta.replaceChildren(
      el("span", {}, el("span", { class: `status-dot ${calibrating ? "warn" : "ok"}` }),
        calibrating ? "Model calibrating" : "Live"),
      el("span", { class: "mono-sm", text: `Week ${health.week ?? "—"} · ${health.season}` }),
      el("span", { class: "mono-sm",
        text: `${health.database.predictions.toLocaleString()} predictions recorded, `
            + `${health.database.settled_predictions.toLocaleString()} settled` }),
      el("span", { class: "mono-sm", text: `v${health.version}` }));
  } catch {
    foot.replaceChildren(el("span", { class: "status-dot bad" }), "server unreachable");
    meta.replaceChildren(el("span", { class: "neg", text: "Server unreachable" }));
  }
}

async function refreshSession() {
  const foot = document.getElementById("footUser");
  try {
    const me = await api("/api/me");
    session.user = me.user;
    foot.replaceChildren(me.user
      ? el("span", {}, `signed in as ${me.user}`)
      : el("a", { href: "#", text: "sign in", onClick: (e) => { e.preventDefault(); openLogin(); } }));
  } catch {
    foot.textContent = "";
  }
}

/* --------------------------------------------------------------------- login */

function openLogin() { document.getElementById("loginGate").hidden = false; }
function closeLogin() { document.getElementById("loginGate").hidden = true; }

function wireLogin() {
  const gate = document.getElementById("loginGate");
  const userField = document.getElementById("liUser");
  const passField = document.getElementById("liPass");
  const errorField = document.getElementById("liError");

  const submit = async (path) => {
    errorField.textContent = "";
    try {
      const result = await api(path, {
        method: "POST",
        body: { username: userField.value.trim(), password: passField.value },
      });
      session.user = result.user;
      closeLogin();
      await refreshSession();
      render();
    } catch (err) {
      errorField.textContent = err.message;
    }
  };

  document.getElementById("liLogin").addEventListener("click", () => submit("/api/login"));
  document.getElementById("liSignup").addEventListener("click", () => submit("/api/signup"));
  document.getElementById("liSkip").addEventListener("click", closeLogin);
  passField.addEventListener("keydown", (e) => { if (e.key === "Enter") submit("/api/login"); });
  gate.addEventListener("click", (e) => { if (e.target === gate) closeLogin(); });
}

/* -------------------------------------------------------------------- startup */

async function boot() {
  applyTheme();
  buildNav();
  wireLogin();
  window.addEventListener("hashchange", () => { highlightNav(); render(); });
  await Promise.all([refreshStatus(), refreshSession()]);
  await render();
  // Keep the header status honest without re-rendering the view under the user.
  setInterval(refreshStatus, 60_000);
}

boot();
