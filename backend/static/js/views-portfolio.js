/* Parlays, portfolio, and settings. */

import {
  api, el, frag, panel, stat, statRow, badge, table, loading, emptyState, errorState,
  notice, disclosure, pct, signedPct, num, money, cents, evClass, get, relativeTime,
} from "./core.js?v=2.0.6";
import { equityChart } from "./charts.js?v=2.0.6";

/* -------------------------------------------------------------------- parlays */

export async function parlays(mount, { navigate }) {
  mount.replaceChildren(loading(6));
  let data;
  try { data = await api("/api/parlays"); }
  catch (err) { mount.replaceChildren(errorState(err, () => parlays(mount, { navigate }))); return; }

  const blocks = [
    notice(data.disclaimer, "warn"),
    ...(data.warnings || []).map((w) => notice(w, "warn")),
  ];

  for (const category of data.categories) {
    blocks.push(panel(category.name || category.category, {
      sub: category.parlays.length
        ? `${category.parlays.length} qualifying`
        : "none on this board",
    },
      el("div", { class: "prose", style: "margin-bottom:12px", text: category.description || "" }),
      category.parlays.length
        ? el("div", { style: "display:flex;flex-direction:column;gap:14px" },
            ...category.parlays.map((p) => parlayCard(p, navigate)))
        : emptyState("No parlay at this risk level", category.note)));
  }
  mount.replaceChildren(frag(...blocks));
}

export function parlayCard(parlay, navigate) {
  const correlated = Math.abs(parlay.correlation_effect) >= 0.005;
  return el("div", { class: "card" },
    el("div", { class: "card-top" },
      el("div", {},
        el("div", { class: "card-title",
          text: `${parlay.leg_count}-leg ${parlay.category} parlay · ${num(parlay.payout_multiple, 2)}x` }),
        el("div", { class: "card-sub",
          text: `Wins about ${pct(parlay.model_probability, 1)} of the time` })),
      el("div", { class: "card-badges" },
        badge(parlay.risk_rating, parlay.risk_rating === "moderate" ? "medium" : "warn"),
        badge(`${signedPct(parlay.ev_per_dollar)} EV`, parlay.ev_per_dollar > 0 ? "high" : "low"))),

    el("div", { class: "probrow" },
      el("div", { class: "probcell" },
        el("div", { class: "k", text: "Model combined" }),
        el("div", { class: "v", text: pct(parlay.model_probability, 1) })),
      el("div", { class: "probcell" },
        el("div", { class: "k", text: "Market implied" }),
        el("div", { class: "v", text: pct(parlay.market_implied_probability, 1) })),
      el("div", { class: "probcell" },
        el("div", { class: "k", text: "Estimated edge" }),
        el("div", { class: `v ${evClass(parlay.estimated_edge)}`,
                    text: signedPct(parlay.estimated_edge, 1) }))),

    // Built with the shared table helper rather than nested el() calls: the hand-rolled
    // version was six levels deep and had a genuine unbalanced-paren bug in it.
    table([
      { label: "Leg", key: "label" },
      { label: "Game", render: (leg) =>
          el("a", { href: `#/game/${leg.game_id}`, class: "mono-sm", text: leg.game_id }) },
      { label: "Model", num: true, render: (leg) => pct(leg.model_prob) },
      { label: "Market", num: true, render: (leg) => pct(leg.market_prob) },
      { label: "Cost", num: true, render: (leg) => cents(leg.cost) },
      { label: "Conf", render: (leg) => badge(leg.confidence, leg.confidence) },
    ], parlay.legs),

    correlated
      ? notice(`<strong>Correlation matters here.</strong> Multiplying the legs would give `
             + `${pct(parlay.naive_multiplied_probability, 1)}; simulating the games jointly gives `
             + `${pct(parlay.model_probability, 1)}.`)
      : null,

    parlay.explanation?.length
      ? el("ul", { class: "reasons" }, ...parlay.explanation.map((e) => el("li", { text: e })))
      : null,

    parlay.warnings?.length
      ? el("ul", { class: "reasons" }, ...parlay.warnings.map((w) =>
          el("li", { class: "warnc", text: w })))
      : null,

    el("div", { class: "card-foot" },
      el("span", { class: "mono-sm",
        text: `combined cost ${cents(parlay.combined_cost_per_dollar)} per $1 of payout` }),
      el("div", { class: "spacer" }),
      el("button", { class: "btn sm primary", text: "Paper-place all legs",
        onClick: (e) => placeParlay(e.currentTarget, parlay) })),
  );
}

async function placeParlay(button, parlay) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Placing…";
  try {
    const result = await api("/api/parlays/place", {
      method: "POST",
      body: {
        legs: parlay.legs.map((l) => ({
          ticker: l.ticker, side: "yes", game_id: l.game_id,
          label: l.label, market_type: l.market_type, model_prob: l.model_prob,
        })),
        stake: 10, mode: "paper", category: parlay.category,
        combined_prob: parlay.model_probability,
        combined_odds: parlay.payout_multiple,
        ev_per_dollar: parlay.ev_per_dollar,
        risk_rating: parlay.risk_rating,
      },
    });
    button.textContent = result.complete
      ? `✓ ${result.filled} legs placed`
      : `${result.filled}/${result.requested} filled`;
    button.title = result.note;
    button.classList.remove("primary");
  } catch (err) {
    button.textContent = err.status === 401 ? "Sign in first" : "Failed";
    button.title = err.message;
    setTimeout(() => { button.textContent = original; button.disabled = false; }, 2500);
  }
}

/* ------------------------------------------------------------------ portfolio */

export async function portfolio(mount, { navigate }) {
  mount.replaceChildren(loading(6));
  let data;
  try { data = await api("/api/portfolio"); }
  catch (err) {
    mount.replaceChildren(err.status === 401
      ? emptyState("Sign in required",
          "Portfolio, exposure limits and position tracking need an account.")
      : errorState(err, () => portfolio(mount, { navigate })));
    return;
  }

  const risk = data.risk;
  const open = data.open_positions || [];
  const closed = data.closed_positions || [];
  const dd = risk.drawdown;

  mount.replaceChildren(frag(
    notice(risk.disclaimer, "warn"),
    statRow(
      stat("Bankroll", money(risk.settings.bankroll), `${risk.settings.mode} sizing`),
      stat("At risk", money(risk.exposure.total), `${open.length} open`),
      stat("Today", money(risk.exposure.today),
           `${money(risk.limits.daily_remaining)} of daily budget left`),
      stat("Realised P&L", money(data.realised_pnl), `${closed.length} closed`,
           evClass(data.realised_pnl)),
      stat("Max drawdown", money(dd.max_drawdown), pct(dd.max_drawdown_pct), "neg"),
    ),

    data.execution.live_available
      ? notice("<strong>Live trading is enabled for this account.</strong> Orders placed from this "
             + "interface will use real money on Kalshi, subject to the hard caps below.", "bad")
      : notice(`Paper mode. ${data.execution.reason}. Hard caps: `
             + `${money(data.execution.hard_max_stake)} per order, `
             + `${money(data.execution.hard_daily_cap)} per day.`),

    panel("Open positions", { sub: `${open.length}`, flush: true },
      open.length
        ? table([
            { label: "Position", key: "label" },
            { label: "Game", render: (r) => el("a", { href: `#/game/${r.game_id}`, class: "mono-sm",
                                                      text: r.game_id || "—" }) },
            { label: "Mode", render: (r) => badge(r.mode, r.mode === "live" ? "live" : "paper") },
            { label: "Side", key: "side" },
            { label: "Contracts", num: true, key: "contracts" },
            { label: "Entry", num: true, render: (r) => cents(r.entry_price) },
            { label: "Stake", num: true, render: (r) => money(r.stake) },
            { label: "Opened", render: (r) => relativeTime(r.created_at) },
            { label: "", render: (r) => el("button", { class: "btn sm", text: "Close",
                onClick: (e) => closePosition(e.currentTarget, r.id, () => portfolio(mount, { navigate })) }) },
          ], open)
        : emptyState("No open positions",
            "Take a paper position from a prediction card and it will show up here.")),

    panel("Exposure limits", { sub: "caps the interface will not let you exceed" },
      el("dl", { class: "kv" },
        el("dt", { text: "Per bet" }), el("dd", { text: money(risk.limits.per_bet) }),
        el("dt", { text: "Per game" }), el("dd", { text: money(risk.limits.per_game) }),
        el("dt", { text: "Per team" }), el("dd", { text: money(risk.limits.per_team) }),
        el("dt", { text: "Daily remaining" }), el("dd", { text: money(risk.limits.daily_remaining) })),
      Object.keys(risk.exposure.by_game).length
        ? frag(el("hr", { class: "rule" }),
            table([
              { label: "Game", render: (r) => el("a", { href: `#/game/${r[0]}`, class: "mono-sm", text: r[0] }) },
              { label: "Exposure", num: true, render: (r) => money(r[1]) },
            ], Object.entries(risk.exposure.by_game)))
        : null),

    panel("Equity curve", { sub: "realised, closed positions only" },
      dd.equity_curve.length > 1
        ? equityChart(dd.equity_curve.map((p) => ({ cumulative: p.equity - dd.starting_bankroll })))
        : emptyState("Not enough history",
            "The curve appears once positions have been closed or settled.")),

    panel("Parlays", { flush: true },
      data.parlays.length
        ? table([
            { label: "Placed", render: (r) => relativeTime(r.created_at) },
            { label: "Category", key: "category" },
            { label: "Legs", num: true, key: "leg_count" },
            { label: "Model", num: true, render: (r) => pct(r.combined_prob) },
            { label: "Payout", num: true, render: (r) => `${num(r.combined_odds, 2)}x` },
            { label: "Stake", num: true, render: (r) => money(r.stake) },
            { label: "Status", render: (r) => badge(r.status, "low") },
          ], data.parlays)
        : emptyState("No parlays placed", "Parlays you place appear here with their legs.")),

    panel("Closed positions", { flush: true },
      closed.length
        ? table([
            { label: "Position", key: "label" },
            { label: "Entry", num: true, render: (r) => cents(r.entry_price) },
            { label: "Exit", num: true, render: (r) => cents(r.exit_price) },
            { label: "Stake", num: true, render: (r) => money(r.stake) },
            { label: "P&L", num: true, cls: (r) => evClass(r.pnl), render: (r) => money(r.pnl) },
            { label: "Closed", render: (r) => relativeTime(r.closed_at) },
          ], closed)
        : emptyState("Nothing closed yet", "Settled and closed positions appear here.")),
  ));
}

async function closePosition(button, positionId, refresh) {
  button.disabled = true;
  button.textContent = "Closing…";
  try {
    await api("/api/positions/close", { method: "POST", body: { position_id: positionId } });
    refresh();
  } catch (err) {
    button.textContent = "Failed";
    button.title = err.message;
  }
}

/* ------------------------------------------------------------------- settings */

export async function settings(mount, { navigate, session }) {
  mount.replaceChildren(loading(4));
  let health, portfolioData = null;
  try {
    health = await api("/api/health");
    try { portfolioData = await api("/api/portfolio"); } catch { /* signed out is fine */ }
  } catch (err) {
    mount.replaceChildren(errorState(err, () => settings(mount, { navigate, session })));
    return;
  }

  const risk = portfolioData?.risk?.settings;
  const form = el("div", { class: "controls" });
  if (risk) {
    const fields = [
      ["bankroll", "Bankroll ($)", risk.bankroll, 1],
      ["kelly_fraction", "Kelly fraction", risk.kelly_fraction, 0.05],
      ["max_stake_pct", "Max per bet (fraction)", risk.max_stake_pct, 0.01],
      ["max_per_game_pct", "Max per game", risk.max_per_game_pct, 0.01],
      ["max_per_team_pct", "Max per team", risk.max_per_team_pct, 0.01],
      ["max_daily_pct", "Max per day", risk.max_daily_pct, 0.01],
    ];
    for (const [key, label, value, step] of fields) {
      form.append(el("label", { class: "field" }, label,
        el("input", { type: "number", step, value, id: `set_${key}`, style: "width:120px" })));
    }
    form.append(el("label", { class: "field" }, "Sizing mode",
      el("select", { id: "set_mode" },
        ...["kelly", "percentage", "flat"].map((m) =>
          el("option", { value: m, selected: m === risk.mode, text: m })))));
    form.append(el("button", { class: "btn primary", text: "Save",
      onClick: async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true; btn.textContent = "Saving…";
        const body = { mode: document.getElementById("set_mode").value };
        for (const [key] of fields) {
          const raw = parseFloat(document.getElementById(`set_${key}`).value);
          if (!Number.isNaN(raw)) body[key === "bankroll" ? "current" : key] = raw;
        }
        try {
          await api("/api/bankroll", { method: "POST", body });
          btn.textContent = "✓ Saved";
        } catch (err) {
          btn.textContent = "Failed"; btn.title = err.message;
        }
        setTimeout(() => { btn.disabled = false; btn.textContent = "Save"; }, 1800);
      } }));
  }

  const sources = Object.entries(health.data_sources).map(([name, s]) => ({ name, ...s }));

  mount.replaceChildren(frag(
    panel("Account", {},
      session.user
        ? frag(
            el("div", { class: "prose", html: `Signed in as <strong>${session.user}</strong>.` }),
            el("div", { style: "margin-top:10px" },
              el("button", { class: "btn", text: "Sign out", onClick: async () => {
                await api("/api/logout", { method: "POST" });
                location.reload();
              } })))
        : el("div", { class: "prose" },
            "Not signed in. Research is readable without an account; portfolio and sizing are not.")),

    risk ? panel("Risk and bankroll", {
      sub: "these limits are enforced server-side, not just in the interface" }, form) : null,

    panel("Data sources", { sub: "what is configured, and what needs a key", flush: true },
      table([
        { label: "Source", render: (s) => s.name.replace(/_/g, " ") },
        { label: "Status", render: (s) => (s.configured
            ? badge("configured", "high")
            : badge(s.requires_key ? "needs a key" : "unavailable", "warn")) },
        { label: "Key required", render: (s) => (s.requires_key ? "yes" : "no") },
        { label: "Note", render: (s) => s.note || (s.requires_key ? "" : "Free and public — no account needed.") },
      ], sources)),

    panel("Trading mode", {},
      el("div", { class: "prose" },
        health.data_sources.kalshi_trading.configured
          ? "Kalshi API credentials are configured. Live orders additionally require "
            + "LIVE_TRADING_ENABLED=true and LIVE_TRADING_USER to match your login."
          : "No Kalshi API credentials are configured, so every order is placed on paper. "
            + "Add KALSHI_KEY_ID and KALSHI_PRIVATE_KEY to enable live trading. Market data "
            + "does not need them.")),

    panel("Background jobs", { sub: "keep the board warm and record history", flush: true },
      table([
        { label: "Job", render: (j) => j[0] },
        { label: "Runs", num: true, render: (j) => j[1].runs ?? 0 },
        { label: "Errors", num: true, cls: (j) => (j[1].errors ? "neg" : ""), render: (j) => j[1].errors ?? 0 },
        { label: "Last run", render: (j) => relativeTime(j[1].last_run) },
        { label: "Last result", render: (j) => el("span", { class: "mono-sm",
            text: j[1].last_error || JSON.stringify(j[1].last_result || {}).slice(0, 90) }) },
      ], Object.entries(health.jobs.jobs || {}))),

    panel("Appearance", {},
      el("div", { class: "seg" },
        ...["system", "dark", "light"].map((mode) =>
          el("button", {
            class: (localStorage.getItem("theme") || "system") === mode ? "active" : "",
            text: mode,
            onClick: (e) => {
              localStorage.setItem("theme", mode);
              applyTheme();
              for (const b of e.currentTarget.parentElement.children) b.classList.remove("active");
              e.currentTarget.classList.add("active");
            },
          })))),
  ));
}

export function applyTheme() {
  let mode = "system";
  try { mode = localStorage.getItem("theme") || "system"; } catch { /* private mode */ }
  if (mode === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", mode);
}
