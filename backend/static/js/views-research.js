/* Dashboard, slate, game detail, predictions and the raw market board. */

import {
  api, el, frag, panel, stat, statRow, badge, table, loading, emptyState, errorState,
  notice, probRow, disclosure, confidenceBadge, pct, signedPct, num, signed, money,
  cents, kickoffLabel, relativeTime, evClass, get, setChildren,
} from "./core.js?v=2.1.4";
import { equityChart, distributionChart, movementChart, splitBar, rankBars } from "./charts.js?v=2.1.4";
import { parlayCard } from "./views-portfolio.js?v=2.1.4";

const teamAbbr = (g, side) => g?.[side] ?? "?";

/* ------------------------------------------------------------------ dashboard */

export async function dashboard(mount, { navigate }) {
  mount.replaceChildren(loading(6));
  let slate, opps, parlayBoard, movers, performance;
  try {
    [slate, opps, parlayBoard, movers, performance] = await Promise.all([
      api("/api/slate"),
      api("/api/opportunities?limit=6"),
      api("/api/parlays"),
      api("/api/market-movers?limit=8"),
      api("/api/performance"),
    ]);
  } catch (err) {
    mount.replaceChildren(errorState(err, () => dashboard(mount, { navigate })));
    return;
  }

  const upcoming = slate.games.filter((g) => !g.game.completed);
  const valueCount = slate.games.reduce((sum, g) => sum + g.value_count, 0);
  const best = parlayBoard.categories.find((c) => c.category === "best");
  const bestParlay = best?.parlays?.[0] || null;
  const record = performance.overall;

  const blocks = [
    ...(slate.warnings || []).map((w) => notice(w, "warn")),
    statRow(
      stat("Slate", `Week ${slate.week}`, `${upcoming.length} games · ${slate.season} season`),
      stat("Priced contracts", String(get(slate, "board.contracts_priced", 0)),
           `${get(slate, "board.events_seen", 0)} Kalshi events read`),
      stat("Edges found", String(valueCount),
           valueCount ? "clearing every discipline gate" : "no qualifying edge right now",
           valueCount ? "pos" : ""),
      stat("Model confidence", pct(get(slate, "ratings.confidence", 0), 0),
           `${get(slate, "ratings.effective_games_per_team", 0)} effective games/team`),
      stat("Settled record", record.n ? `${record.n}` : "0",
           record.n ? `Brier ${num(record.brier, 4)} vs market ${num(record.brier_market, 4)}`
                    : "predictions recorded, awaiting results"),
    ),

    panel("Today's slate", { sub: `Week ${slate.week}`, actions:
      el("button", { class: "btn sm", onClick: () => navigate("#/games"), text: "All games →" }) },
      slateTable(upcoming.slice(0, 8), navigate)),

    el("div", { class: "grid cols-2" },
      panel("Best current opportunities", { sub: "ranked by expected return" },
        opps.opportunities.length
          ? opportunityTable(opps.opportunities, navigate)
          : emptyState("No qualifying edges", opps.empty_reason || "Nothing clears the gates right now.")),

      panel("Market movers", { sub: "largest 24h price moves" },
        movers.movers.length
          ? table([
              { label: "Contract", render: (m) => el("div", {},
                  el("div", { text: m.label }),
                  el("div", { class: "mono-sm", text: m.game_id })) },
              { label: "Now", num: true, render: (m) => pct(m.current, 0) },
              { label: "24h", num: true, cls: (m) => evClass(m.move_24h),
                render: (m) => signedPct(m.move_24h, 1) },
              { label: "", render: (m) => (m.unusual ? badge("unusual", "warn") : "") },
            ], movers.movers, { onRowClick: (m) => m.game_id && navigate(`#/game/${m.game_id}`) })
          : emptyState("No movement recorded yet", movers.note))),

    panel("Model's best parlay", { sub: bestParlay ? `${bestParlay.leg_count} legs` : "none today",
      actions: el("button", { class: "btn sm", onClick: () => navigate("#/parlays"), text: "All parlays →" }) },
      bestParlay ? parlayCard(bestParlay, navigate)
                 : emptyState("No parlay today", best?.note || "Nothing qualifies.")),

    el("div", { class: "grid cols-2" },
      panel("Model performance", { sub: "live tracked record" },
        record.n
          ? frag(
              statRow(
                stat("Settled", String(record.n)),
                stat("Hit rate", pct(record.hit_rate)),
                stat("Brier", num(record.brier, 4), `market ${num(record.brier_market, 4)}`),
                stat("ROI", signedPct(get(record, "roi.roi", 0)), "flat stakes",
                     evClass(get(record, "roi.roi", 0))),
              ),
              equityChart(performance.equity_curve))
          : emptyState("No settled predictions yet", performance.empty_reason || "")),

      panel("Portfolio", { sub: "open paper exposure" }, portfolioSummary())),

    notice("<strong>A model edge is an estimate, not a promise.</strong> Every position here can lose, "
         + "and the model has not yet demonstrated an edge over closing sportsbook lines in backtest. "
         + "See <a href=\"#/backtests\">Backtests</a> for the measured verdict."),
  ];
  mount.replaceChildren(frag(...blocks));
}

function portfolioSummary() {
  const holder = el("div", {}, loading(3));
  api("/api/portfolio")
    .then((p) => {
      const open = p.open_positions || [];
      holder.replaceChildren(
        statRow(
          stat("Bankroll", money(get(p, "risk.settings.bankroll", 0))),
          stat("At risk", money(get(p, "risk.exposure.total", 0)),
               `${open.length} open position${open.length === 1 ? "" : "s"}`),
          stat("Realised P&L", money(p.realised_pnl), "closed positions",
               evClass(p.realised_pnl)),
        ),
        open.length
          ? table([
              { label: "Position", key: "label" },
              { label: "Mode", render: (r) => badge(r.mode, r.mode === "live" ? "live" : "paper") },
              { label: "Stake", num: true, render: (r) => money(r.stake) },
              { label: "Entry", num: true, render: (r) => cents(r.entry_price) },
            ], open.slice(0, 5))
          : emptyState("No open positions",
              "Paper positions you take from a prediction card will appear here."));
    })
    .catch((err) => {
      holder.replaceChildren(
        err.status === 401
          ? emptyState("Not signed in", "Sign in to track a portfolio and size positions.")
          : errorState(err));
    });
  return holder;
}

/** The dashboard's ranked-edge table.
 *
 * This function was referenced from the dashboard for a long time without existing: the
 * call sits behind `if (opps.opportunities.length)`, and until the sportsbook consensus
 * started finding edges that branch never ran. The app beginning to work is what exposed
 * it, which is a good argument for smoke-testing against live data rather than fixtures.
 */
function opportunityTable(rows, navigate) {
  return table([
    { label: "Market", render: (o) => el("div", {},
        el("div", { text: o.label }),
        el("div", { class: "mono-sm", text: `${o.game_id} · ${o.market_type.replace(/_/g, " ")}` })) },
    { label: "Fair", num: true, render: (o) => pct(o.features?.fair_prob ?? o.model_prob) },
    { label: "Kalshi", num: true, render: (o) => pct(o.market_prob) },
    { label: "EV", num: true, cls: (o) => evClass(o.ev_per_dollar),
      render: (o) => signedPct(o.ev_per_dollar) },
    { label: "Conf", render: (o) => confidenceBadge(o.confidence) },
    { label: "Depth", num: true, render: (o) => money(o.features?.depth_usd) },
    { label: "Size", num: true, render: (o) => (o.sizing
        ? `${o.sizing.contracts}x = ${money(o.sizing.tradeable_cost)}`
        : "—") },
  ], rows, { onRowClick: (o) => navigate(`#/game/${o.game_id}`) });
}

/* ---------------------------------------------------------------------- slate */

function slateTable(games, navigate) {
  if (!games.length) return emptyState("No upcoming games", "The next slate has not been posted yet.");
  return table([
    { label: "Kickoff", render: (g) => el("span", { class: "mono-sm", text: kickoffLabel(g.game.kickoff) }) },
    { label: "Matchup", render: (g) => el("div", { class: "matchup" },
        el("span", { class: "abbr", text: g.game.away }),
        el("span", { class: "at", text: "@" }),
        el("span", { class: "abbr", text: g.game.home })) },
    { label: "Projected", num: true, render: (g) =>
        `${num(get(g, "projection.projected_score.away", 0), 1)}–${num(get(g, "projection.projected_score.home", 0), 1)}` },
    { label: "Margin", num: true, render: (g) => signed(get(g, "projection.expected_margin", 0), 1) },
    { label: "Total", num: true, render: (g) => num(get(g, "projection.expected_total", 0), 1) },
    { label: "Model win", num: true, render: (g) => {
        const hw = get(g, "projection.win_probability.home", 0);
        return `${g.game.home} ${pct(hw, 0)}`;
      } },
    { label: "Markets", num: true, key: "market_count" },
    { label: "Edges", num: true, cls: (g) => (g.value_count ? "pos" : "dim"),
      render: (g) => String(g.value_count) },
  ], games, { onRowClick: (g) => navigate(`#/game/${g.game.game_id}`) });
}

export async function games(mount, { navigate }) {
  mount.replaceChildren(loading(8));
  let slate;
  try { slate = await api("/api/slate"); }
  catch (err) { mount.replaceChildren(errorState(err, () => games(mount, { navigate }))); return; }

  mount.replaceChildren(frag(
    ...(slate.warnings || []).map((w) => notice(w, "warn")),
    notice(`Board read from Kalshi: <strong>${get(slate, "board.contracts_priced", 0)}</strong> contracts priced `
         + `across <strong>${get(slate, "board.events_seen", 0)}</strong> events. `
         + `${get(slate, "board.events_out_of_slate", 0)} events belong to later weeks. `
         + `${get(slate, "board.events_unmapped", 0)} could not be matched to a scheduled game.`),
    panel(`Week ${slate.week} — ${slate.season}`, { sub: `${slate.games.length} games`, flush: true },
      slateTable(slate.games, navigate)),
  ));
}

/* ---------------------------------------------------------------- game detail */

export async function gameDetail(mount, { navigate, params }) {
  mount.replaceChildren(loading(8));
  let g;
  try {
    // Game lines only. Player props mean an order-book read per contract — around 210 more
    // for a single game — which turns a one-second page into a twenty-second one. They are
    // fetched on demand instead.
    g = await api(`/api/games/${encodeURIComponent(params.gameId)}?include_props=false`);
  } catch (err) {
    mount.replaceChildren(errorState(err, () => gameDetail(mount, { navigate, params })));
    return;
  }

  const proj = g.projection;
  const base = g.base_projection;
  const home = g.teams.home, away = g.teams.away;
  const adj = g.adjustment;

  const marketBySide = {};
  for (const m of g.markets) if (m.market_type === "moneyline" && m.team) marketBySide[m.team] = m;

  mount.replaceChildren(frag(
    el("div", { class: "controls" },
      el("button", { class: "btn sm", onClick: () => navigate("#/games"), text: "← All games" }),
      el("span", { class: "mono-sm", text: kickoffLabel(g.game.kickoff) }),
      g.game.div_game ? badge("divisional") : null,
      badge(g.game.roof || "outdoors"),
      g.weather?.indoor ? badge("indoor") : null),

    statRow(
      stat("Projected score",
           `${num(proj.projected_score.away, 1)}–${num(proj.projected_score.home, 1)}`,
           `${away.name} at ${home.name}`),
      stat("Expected margin", signed(proj.expected_margin, 1),
           proj.expected_margin > 0 ? `${home.name} favoured` : `${away.name} favoured`),
      stat("Expected total", num(proj.expected_total, 1),
           `sigma ${num(proj.total_sigma, 1)}`),
      stat(`${home.name} win`, pct(proj.win_probability.home),
           marketBySide[g.game.home]?.mid != null
             ? `market ${pct(marketBySide[g.game.home].mid)}` : "no market price"),
      stat(`${away.name} win`, pct(proj.win_probability.away),
           marketBySide[g.game.away]?.mid != null
             ? `market ${pct(marketBySide[g.game.away].mid)}` : "no market price"),
      stat("Confidence", pct(proj.confidence, 0), "model + data quality"),
    ),

    el("div", { class: "grid cols-2" },
      panel("Margin distribution", { sub: "home margin · key numbers shaded darker" },
        distributionChart(proj.margin_distribution, {
          marker: g.game.spread_line ?? null,
          markerLabel: g.game.spread_line != null
            ? `Closing consensus line ${signed(g.game.spread_line, 1)}` : "",
        }),
        el("div", { class: "prose", style: "margin-top:10px" },
           `A margin of exactly 3 is far more likely than a smooth model implies, which is why `
         + `a −2.5 and a −3.5 are priced differently here. Most likely margins: `
         + proj.most_likely_margins.map((m) => `${m.margin} (${pct(m.prob, 1)})`).join(", ") + ".")),

      panel("Win probability", { sub: "model vs market" },
        splitBar(proj.win_probability.home, proj.win_probability.away, home.name, away.name),
        el("div", { class: "prose", style: "margin-top:12px" },
          `Ties carry ${pct(proj.win_probability.tie, 2)} and void the Kalshi moneyline, so the `
        + `two sides above are renormalised over decisive outcomes.`),
        el("hr", { class: "rule" }),
        el("div", { class: "stat-label", text: "Total points distribution" }),
        distributionChart(proj.total_distribution, {
          height: 120,
          marker: g.game.total_line ?? null,
          markerLabel: g.game.total_line != null
            ? `Closing consensus total ${num(g.game.total_line, 1)}` : "",
        }))),

    adj.reasons.length || adj.margin_shift || adj.total_shift
      ? panel("Adjustments applied", { sub: `${signed(adj.margin_shift, 1)} pts margin · ${signed(adj.total_shift, 1)} pts total` },
          el("div", { class: "prose" },
            `Base model projected ${signed(base.expected_margin, 1)} margin and `
          + `${num(base.expected_total, 1)} total. After injuries, situational spots and weather: `
          + `${signed(proj.expected_margin, 1)} and ${num(proj.expected_total, 1)}.`
          + (adj.sigma_add > 0.05
              ? ` Uncertainty widened by ${num(adj.sigma_add, 1)} points because key availability is unresolved.`
              : "")),
          el("ul", { class: "reasons", style: "margin-top:10px" },
            ...adj.reasons.map((r) => el("li", { text: r }))))
      : null,

    el("div", { class: "grid cols-2" },
      panel("Injuries", { sub: "official reports" }, injuryBlock(g)),
      panel("Situational + weather", {}, situationalBlock(g))),

    panel("Team comparison", { sub: "opponent-adjusted, league rank in brackets", flush: true },
      comparisonTable(g)),

    panel("Matchup model", { sub: "phase-weighted unit edges" },
      el("div", { class: "prose" },
        `Matchup-weighted margin ${signed(get(g, "matchup.margin", 0), 1)} against the core model's `
      + `${signed(base.expected_margin, 1)}.`),
      el("ul", { class: "reasons", style: "margin-top:10px" },
        ...(get(g, "matchup.reasons", []) || []).map((r) => el("li", { text: r })))),

    panel("Opportunities", { sub: `${g.opportunities.length} clearing every gate`, flush: true },
      g.opportunities.length
        ? el("div", { class: "cards", style: "padding:14px" },
            ...g.opportunities.map((s) => predictionCard(s, navigate)))
        : emptyState("No qualifying edges in this game",
            "Every market here is either fairly priced, too illiquid to trade, or outside the "
          + "sane price band. That is the normal outcome.")),

    panel("Market board", { sub: `${g.markets.length} priced contracts`, flush: true },
      marketTable(g.markets)),

    g.mispricing.violations.length
      ? panel("Internal market inconsistencies", { sub: g.mispricing.note },
          table([
            { label: "Market", key: "market" },
            { label: "Detail", key: "detail" },
            { label: "Size", num: true, render: (v) => signedPct(v.size) },
            { label: "Tradeable", render: (v) => (v.tradeable ? badge("yes", "high") : badge("thin", "reference")) },
          ], g.mispricing.violations))
      : null,

    bookConsensusPanel(g),

    lineMovementPanel(g),

    playerPropsPanel(params.gameId),

    panel("Model reasoning", { sub: "largest rating gaps" },
      rankBars((proj.drivers || []).slice(0, 8).map((d) => ({
        label: d.edge_to, value: d.edge_to === g.game.home ? d.gap : -d.gap,
      }))),
      el("div", { class: "prose", style: "margin-top:12px" },
        (proj.drivers || []).slice(0, 4)
          .map((d) => `${d.label}: edge to ${d.edge_to} (${num(d.gap, 3)})`).join(" · "))),

    panel("Prediction history for this game", { sub: "recorded before outcomes, never edited", flush: true },
      g.prediction_history.length
        ? table([
            { label: "Recorded", render: (r) => relativeTime(r.created_at) },
            { label: "Strategy", key: "strategy" },
            { label: "Selection", key: "selection" },
            { label: "Model", num: true, render: (r) => pct(r.fair_prob ?? r.model_prob) },
            { label: "Market", num: true, render: (r) => pct(r.market_prob) },
            { label: "Edge", num: true, cls: (r) => evClass(r.edge), render: (r) => signedPct(r.edge) },
            { label: "Status", render: (r) => badge(r.status, r.status === "settled" ? "medium" : "low") },
          ], g.prediction_history.slice(0, 40))
        : emptyState("Nothing recorded yet",
            "Predictions are written by the background job every few minutes.")),
  ));
}

function playerPropsPanel(gameId) {
  const body = el("div", {},
    el("div", { class: "prose" },
      "Player props are priced on request. Each contract needs its own order-book read and "
    + "a game carries a couple of hundred of them, so loading them with the page would cost "
    + "about twenty seconds."),
    el("div", { style: "margin-top:12px" },
      el("button", { class: "btn primary", text: "Load player props", onClick: load })));

  async function load(event) {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "Reading order books…";
    try {
      const data = await api(`/api/games/${encodeURIComponent(gameId)}?include_props=true`);
      const props = (data.ensemble || []).filter((s) => s.player);
      const priced = props.filter((s) => s.quote && s.quote.cost !== null);
      if (!priced.length) {
        body.replaceChildren(emptyState("No priced player props",
          "Kalshi lists props for this game but none has a readable order book right now."));
        return;
      }
      priced.sort((a, b) => (b.ev_per_dollar ?? -9) - (a.ev_per_dollar ?? -9));
      body.replaceChildren(
        notice("Props are shown for <strong>reference</strong> unless the player is a "
             + "confirmed starter with measured history and no injury designation — and even "
             + "then they are held to roughly double the edge threshold of a game line. The "
             + "model does not know this week's game plan."),
        table([
          { label: "Player", key: "player" },
          { label: "Type", render: (s) => s.market_type.replace(/_/g, " ") },
          { label: "Contract", key: "label" },
          { label: "Model raw", num: true,
            render: (s) => pct(s.features?.raw_model_prob ?? s.model_prob) },
          { label: "Market", num: true, render: (s) => pct(s.market_prob) },
          { label: "Fair", num: true, render: (s) => pct(s.features?.fair_prob ?? s.model_prob) },
          { label: "Edge", num: true, cls: (s) => evClass(s.edge), render: (s) => signedPct(s.edge) },
          { label: "EV", num: true, cls: (s) => evClass(s.ev_per_dollar),
            render: (s) => signedPct(s.ev_per_dollar) },
          { label: "Conf", render: (s) => badge(s.confidence, s.confidence) },
        ], priced.slice(0, 60)),
        el("div", { class: "prose", style: "margin-top:10px" },
          "\u201cModel raw\u201d is the projection's own probability. \u201cFair\u201d is that "
        + "blended toward the traded price, and it is what the edge is computed from — the "
        + "blend weight is deliberately low because the backtest says the model has not "
        + "earned the right to override a price."));
    } catch (err) {
      body.replaceChildren(errorState(err));
    }
  }

  return panel("Player props", { sub: "loaded on request" }, body);
}


function bookConsensusPanel(g) {
  const c = g.book_consensus;
  if (!c) {
    return panel("Sportsbook consensus", { sub: "cross-check on Kalshi" },
      emptyState("No sportsbook consensus for this game",
        "Either the odds feed is unavailable or too few books post this matchup."));
  }
  const proj = g.projection;
  const rows = [
    { what: "Expected margin", books: signed(c.margin, 1), model: signed(proj.expected_margin, 1) },
    { what: "Expected total", books: num(c.total, 1), model: num(proj.expected_total, 1) },
    { what: `${g.teams.home.name} win`, books: pct(c.home_win_prob),
      model: pct(proj.win_probability.home) },
  ];
  return panel("Sportsbook consensus", {
    sub: `${c.book_count} books · they agree within ${num(c.book_disagreement, 1)} pts` },
    notice("This is the strongest evidence on the page, because it does not depend on our "
         + "model being right. It compares Kalshi's price against "
         + `<strong>${c.book_count}</strong> deep sportsbooks. Where a thin exchange contract `
         + "is priced away from that consensus, the exchange is usually the one that is wrong."),
    table([
      { label: "", key: "what" },
      { label: `Sportsbooks (${c.book_count})`, num: true, key: "books" },
      { label: "Our model", num: true, key: "model" },
      { label: "Gap", num: true, render: (r) => {
          const b = parseFloat(String(r.books).replace("%", ""));
          const m = parseFloat(String(r.model).replace("%", ""));
          if (Number.isNaN(b) || Number.isNaN(m)) return "—";
          return signed(m - b, 1);
        } },
    ], rows),
    el("div", { class: "prose", style: "margin-top:10px" },
      `Books quoting: ${(c.books || []).join(", ")}.`));
}


function lineMovementPanel(g) {
  // Only contracts we have actually watched move are worth a sparkline; the rest would be
  // a flat line saying nothing.
  const moved = (g.line_movement || [])
    .filter((m) => m.samples > 2 && Math.abs(m.since_open) >= 0.01)
    .sort((a, b) => Math.abs(b.since_open) - Math.abs(a.since_open))
    .slice(0, 6);

  if (!moved.length) {
    return panel("Line movement", { sub: "from our own stored snapshots" },
      emptyState("No movement recorded for this game yet",
        "Prices are snapshotted every few minutes. Movement appears once a contract has "
      + "been watched long enough to have moved."));
  }

  const byTicker = new Map((g.markets || []).map((m) => [m.ticker, m]));
  return panel("Line movement", { sub: `${moved.length} contracts that have moved` },
    el("div", { class: "grid cols-3" },
      ...moved.map((m) => {
        const quote = byTicker.get(m.ticker);
        return el("div", {},
          el("div", { class: "stat-label", text: quote?.label || m.ticker }),
          el("div", { class: "kv", style: "margin:4px 0 6px" },
            el("dt", { text: "Now" }), el("dd", { text: pct(m.current, 0) }),
            el("dt", { text: "Since open" }),
            el("dd", { class: evClass(m.since_open), text: signedPct(m.since_open) }),
            el("dt", { text: "Range" }),
            el("dd", { text: `${pct(m.low, 0)}–${pct(m.high, 0)}` })),
          movementChart(m.series || []) || el("div", { class: "mono-sm",
            text: `${m.samples} snapshots` }));
      })));
}


function injuryBlock(g) {
  const rows = [...(g.injuries.home || []).map((r) => ({ ...r, side: g.game.home })),
                ...(g.injuries.away || []).map((r) => ({ ...r, side: g.game.away }))];
  if (!rows.length) {
    return emptyState("No injury designations",
      "Neither team has published a game-status designation for this week.");
  }
  rows.sort((a, b) => b.severity - a.severity);
  const impact = g.injuries.impact || {};
  return frag(
    el("div", { class: "prose", style: "margin-bottom:10px" },
      `Estimated impact: ${g.game.home} ${num(get(impact, "home.margin_points", 0), 1)} pts, `
    + `${g.game.away} ${num(get(impact, "away.margin_points", 0), 1)} pts of margin.`),
    table([
      { label: "Team", key: "side" },
      { label: "Player", key: "player" },
      { label: "Pos", key: "position" },
      { label: "Status", render: (r) => r.report_status || r.practice_status || "—" },
      { label: "Injury", key: "injury" },
      { label: "Severity", num: true, render: (r) => num(r.severity, 2) },
    ], rows.slice(0, 14)));
}

function situationalBlock(g) {
  const s = g.situational || {};
  const w = g.weather || {};
  return frag(
    el("dl", { class: "kv" },
      el("dt", { text: "Rest (home / away)" }),
      el("dd", { text: `${s.home_rest ?? "—"} / ${s.away_rest ?? "—"} days` }),
      el("dt", { text: "Away travel" }),
      el("dd", { text: `${num(s.away_travel_miles, 0)} mi` }),
      el("dt", { text: "Timezone shift" }),
      el("dd", { text: `${signed(s.away_timezone_shift, 0)} h` }),
      el("dt", { text: "Divisional" }),
      el("dd", { text: s.divisional ? "yes" : "no" }),
      el("dt", { text: "Roof" }),
      el("dd", { text: s.roof || "—" }),
      el("dt", { text: "Surface" }),
      el("dd", { text: s.surface || "—" })),
    el("hr", { class: "rule" }),
    w.indoor
      ? el("div", { class: "prose", text: w.note || "Indoor game — weather is not a factor." })
      : w.temperature_f !== undefined
        ? el("dl", { class: "kv" },
            el("dt", { text: "Temperature" }), el("dd", { text: `${num(w.temperature_f, 0)}°F` }),
            el("dt", { text: "Wind" }), el("dd", { text: `${num(w.wind_mph, 0)} mph` }),
            el("dt", { text: "Precipitation" }), el("dd", { text: `${num(w.precipitation_pct, 0)}%` }))
        : el("div", { class: "prose", text: "No forecast available for this kickoff yet." }));
}

function comparisonTable(g) {
  return table([
    { label: "Metric", render: (r) => r.metric.replace(/_/g, " ") },
    { label: g.game.away, num: true, render: (r) => `${num(r.away.net, 3)} (${r.away.rank})` },
    { label: g.game.home, num: true, render: (r) => `${num(r.home.net, 3)} (${r.home.rank})` },
    { label: "Edge", render: (r) => {
        const diff = r.home.net - r.away.net;
        return el("span", { class: evClass(diff),
          text: `${diff > 0 ? g.game.home : g.game.away} +${num(Math.abs(diff), 3)}` });
      } },
  ], g.team_comparison);
}

function marketTable(markets) {
  if (!markets.length) {
    return emptyState("No priced contracts",
      "Kalshi lists markets for this game but none has a readable order book right now.");
  }
  const sorted = [...markets].sort((a, b) =>
    a.market_type.localeCompare(b.market_type) || (a.line ?? 0) - (b.line ?? 0));
  return table([
    { label: "Market", render: (m) => m.market_type.replace(/_/g, " ") },
    { label: "Contract", key: "label" },
    { label: "Bid", num: true, render: (m) => cents(m.yes_bid) },
    { label: "Ask", num: true, render: (m) => cents(m.yes_ask) },
    { label: "Mid", num: true, render: (m) => pct(m.mid, 1) },
    { label: "Spread", num: true, render: (m) => cents(m.spread_width) },
    { label: "Depth", num: true, render: (m) => money(m.depth_usd) },
    { label: "Ticker", render: (m) => el("span", { class: "mono-sm", text: m.ticker }) },
  ], sorted);
}

/* ---------------------------------------------------------------- predictions */

const PREDICTION_STATE = {
  sort: "ev", minConfidence: "low", marketType: "", valueOnly: true, includeProps: false,
};

export async function predictions(mount, { navigate }) {
  const render = async () => {
    const body = mount.querySelector("#predBody") || mount;
    body.replaceChildren(loading(6));
    const q = new URLSearchParams({
      sort: PREDICTION_STATE.sort,
      min_confidence: PREDICTION_STATE.minConfidence,
      value_only: String(PREDICTION_STATE.valueOnly),
      include_props: String(PREDICTION_STATE.includeProps),
      limit: "120",
    });
    if (PREDICTION_STATE.marketType) q.set("market_type", PREDICTION_STATE.marketType);
    try {
      const data = await api(`/api/predictions?${q}`);
      body.replaceChildren(
        data.predictions.length
          ? el("div", { class: "cards" }, ...data.predictions.map((s) => predictionCard(s, navigate)))
          : emptyState("No predictions match", data.empty_reason || "Try loosening the filters."));
      const meta = mount.querySelector("#predMeta");
      if (meta) meta.textContent = `${data.count} shown of ${data.total_before_limit}`;
      const sel = mount.querySelector("#marketFilter");
      if (sel && sel.options.length <= 1) {
        for (const m of data.filters.market_types) {
          sel.append(el("option", { value: m, text: m.replace(/_/g, " ") }));
        }
        sel.value = PREDICTION_STATE.marketType;
      }
    } catch (err) {
      body.replaceChildren(errorState(err, render));
    }
  };

  mount.replaceChildren(frag(
    el("div", { class: "controls" },
      el("label", { class: "field" }, "Sort",
        select(["ev", "edge", "confidence", "kickoff", "market", "game"], PREDICTION_STATE.sort,
          (v) => { PREDICTION_STATE.sort = v; render(); })),
      el("label", { class: "field" }, "Min confidence",
        select(["reference", "low", "medium", "high"], PREDICTION_STATE.minConfidence,
          (v) => { PREDICTION_STATE.minConfidence = v; render(); })),
      el("label", { class: "field" }, "Market",
        el("select", { id: "marketFilter", onChange: (e) => {
          PREDICTION_STATE.marketType = e.target.value; render();
        } }, el("option", { value: "", text: "All markets" }))),
      el("label", { class: "inline" },
        el("input", { type: "checkbox", checked: PREDICTION_STATE.valueOnly,
          onChange: (e) => { PREDICTION_STATE.valueOnly = e.target.checked; render(); } }),
        "Value only"),
      el("label", { class: "inline" },
        el("input", { type: "checkbox", checked: PREDICTION_STATE.includeProps,
          onChange: (e) => { PREDICTION_STATE.includeProps = e.target.checked; render(); } }),
        "Include player props"),
      el("span", { id: "predMeta", class: "mono-sm", style: "margin-left:auto" })),
    el("div", { id: "predBody" }),
  ));
  render();
}

function select(options, value, onChange) {
  return el("select", { onChange: (e) => onChange(e.target.value) },
    ...options.map((o) => el("option", { value: o, selected: o === value, text: o })));
}

export function predictionCard(signal, navigate) {
  const f = signal.features || {};
  const q = signal.quote || {};
  const modelProb = f.fair_prob ?? signal.model_prob;

  return el("article", { class: "card" },
    el("div", { class: "card-top" },
      el("div", {},
        el("div", { class: "card-title", text: signal.label }),
        el("div", { class: "card-sub" },
          el("a", { href: `#/game/${signal.game_id}`, text: signal.game_id }),
          ` · ${signal.market_type.replace(/_/g, " ")} · ${signal.strategy}`)),
      el("div", { class: "card-badges" },
        confidenceBadge(signal.confidence),
        f.value ? badge("value", "high") : null)),

    probRow(modelProb, signal.market_prob, signal.edge),

    el("div", { class: "kv" },
      el("dt", { text: "Expected value" }),
      el("dd", { class: evClass(signal.ev_per_dollar), text: `${signedPct(signal.ev_per_dollar)} per $1` }),
      el("dt", { text: "Cost" }),
      el("dd", { text: `${cents(f.cost)} (${num(f.decimal_odds, 2)}x)` }),
      el("dt", { text: "Depth" }),
      el("dd", { text: money(f.depth_usd) }),
      el("dt", { text: "Raw model" }),
      el("dd", { text: pct(f.raw_model_prob ?? signal.model_prob) })),

    signal.reasoning?.length
      ? el("ul", { class: "reasons" }, ...signal.reasoning.slice(0, 4).map((r) => el("li", { text: r })))
      : null,

    signal.confidence === "reference"
      ? notice("Reference only — displayed, never recommended. The model has a known blind spot on this market.")
      : null,

    el("div", { class: "card-foot" },
      el("span", { class: "mono-sm", text: q.ticker || "" }),
      el("div", { class: "spacer" }),
      signal.confidence !== "reference" && q.ticker
        ? el("button", { class: "btn sm primary", text: "Trade",
            onClick: (e) => openTicket(e.currentTarget, signal) })
        : null),
  );
}

/* ------------------------------------------------------------------- order ticket */

/** Whether the OPEN ticket is armed for real money.
 *
 * Reset every time a ticket opens, and that reset is load-bearing. This started as a
 * module-level flag that survived between tickets while each new ticket rendered with
 * "Paper" highlighted — so arming live on one card and then opening another showed Paper
 * and would have placed a real order anyway. A mode indicator that can disagree with the
 * mode is worse than having no indicator. */
let LIVE_ARMED = false;

async function openTicket(button, signal) {
  const card = button.closest(".card");
  if (card.querySelector(".ticket")) { card.querySelector(".ticket").remove(); return; }
  // Every ticket opens on paper, matching the toggle it is about to render.
  LIVE_ARMED = false;

  const f = signal.features || {};
  const cost = f.cost;
  const ticket = el("div", { class: "ticket" }, loading(2));
  card.append(ticket);

  let me = null, sizing = null;
  try {
    me = await api("/api/me");
    const opps = await api("/api/opportunities?limit=60");
    const match = (opps.opportunities || []).find(
      (o) => o.quote && o.quote.ticker === signal.quote.ticker);
    sizing = match?.sizing || null;
  } catch (err) {
    ticket.replaceChildren(errorState(err));
    return;
  }

  if (!me.user) {
    ticket.replaceChildren(notice("Sign in to place orders — paper or live."));
    return;
  }

  const liveAllowed = !!me.live_trading?.allowed;
  const suggested = sizing?.tradeable_cost || (cost ? Number(cost).toFixed(2) : "1.00");
  const stakeInput = el("input", { type: "number", step: "0.01", min: "0.01",
                                   value: suggested, style: "width:92px" });
  const status = el("div", { class: "mono-sm", style: "margin-top:8px" });

  const modeToggle = el("div", { class: "seg" },
    el("button", { class: "active", text: "Paper",
      onClick: (e) => setMode(e, false) }),
    liveAllowed
      ? el("button", { class: "live-mode", text: "LIVE",
                       onClick: (e) => setMode(e, true) })
      : null);

  function setMode(e, live) {
    LIVE_ARMED = live;
    for (const b of e.currentTarget.parentElement.children) b.classList.remove("active");
    e.currentTarget.classList.add("active");
    renderStatus();
  }

  function renderStatus() {
    const stake = parseFloat(stakeInput.value) || 0;
    const contracts = cost ? Math.floor(stake / cost) : 0;
    const parts = [];
    if (contracts < 1) {
      parts.push(`$${stake.toFixed(2)} does not cover one contract at ${cents(cost)}.`);
    } else {
      parts.push(`${contracts} contract${contracts === 1 ? "" : "s"} at ${cents(cost)} `
               + `= $${(contracts * cost).toFixed(2)}. `
               + `Pays $${contracts.toFixed(2)} if it lands.`);
    }
    if (sizing && sizing.stake > 0 && stake > sizing.stake * 2.5) {
      parts.push(`⚠ This edge justifies about $${sizing.stake.toFixed(2)} at your bankroll. `
               + `You are betting ${(stake / sizing.stake).toFixed(1)}x that.`);
    }
    status.replaceChildren(el("span", { text: parts.join(" ") }));
    status.className = "mono-sm" + (LIVE_ARMED ? " warnc" : "");
  }
  stakeInput.addEventListener("input", renderStatus);

  const submit = el("button", { class: "btn sm primary", text: "Place order",
    onClick: () => place() });

  async function place() {
    const stake = parseFloat(stakeInput.value) || 0;
    // Read the mode off the button that is actually highlighted, so what is submitted can
    // never disagree with what the interface is showing.
    const liveButton = modeToggle.querySelector(".live-mode");
    const live = !!(liveButton && liveButton.classList.contains("active"));
    if (live !== LIVE_ARMED) {
      console.warn("mode flag disagreed with the toggle; using the toggle", live, LIVE_ARMED);
    }
    // A real-money order gets an explicit confirmation naming the amount. A paper one
    // does not need it, and asking every time would train the habit of clicking through.
    if (live) {
      const contracts = cost ? Math.floor(stake / cost) : 0;
      const ok = window.confirm(
        `REAL MONEY ORDER\n\n${signal.label}\n`
        + `${contracts} contracts at ${Math.round(cost * 100)}c = $${(contracts * cost).toFixed(2)}\n\n`
        + `This spends real funds on Kalshi and cannot be undone. Continue?`);
      if (!ok) return;
    }
    submit.disabled = true;
    submit.textContent = live ? "Sending to Kalshi…" : "Recording…";
    try {
      const result = await api("/api/orders", {
        method: "POST",
        body: {
          ticker: signal.quote.ticker, side: "yes", stake,
          mode: live ? "live" : "paper",
          game_id: signal.game_id, label: signal.label,
          market_type: signal.market_type,
          model_prob: f.fair_prob ?? signal.model_prob,
        },
      });
      setChildren(ticket,
        notice(result.message, result.ok ? "" : "bad"),
        result.ok
          ? el("div", { class: "mono-sm" },
              `mode: ${result.mode}`
              + (result.order_id ? ` · kalshi order ${result.order_id}` : ""))
          : null,
        el("div", { style: "margin-top:8px" },
          el("a", { href: "#/portfolio", text: "View in portfolio →" })));
    } catch (err) {
      submit.disabled = false;
      submit.textContent = "Place order";
      status.replaceChildren(el("span", { class: "neg", text: err.message }));
    }
  }

  setChildren(ticket,
    el("hr", { class: "rule" }),
    el("div", { class: "controls", style: "margin-bottom:6px" },
      el("label", { class: "field" }, "Stake ($)", stakeInput),
      el("label", { class: "field" }, "Mode", modeToggle),
      submit),
    sizing
      ? el("div", { class: "mono-sm dim" },
          `Model suggests $${sizing.stake.toFixed(2)} `
        + `(${sizing.basis})`
        + (sizing.contracts ? ` → ${sizing.contracts} contract, $${sizing.tradeable_cost.toFixed(2)}` : ""))
      : null,
    sizing?.rounding_note
      ? el("div", { class: "mono-sm dim", style: "margin-top:4px", text: sizing.rounding_note })
      : null,
    status,
    liveAllowed
      ? null
      : el("div", { class: "mono-sm dim", style: "margin-top:6px",
                    text: `Live orders unavailable: ${me.live_trading?.reason || "not enabled"}` }),
  );
  renderStatus();
}

/* -------------------------------------------------------------------- markets */

export async function markets(mount) {
  mount.replaceChildren(loading(8));
  let data;
  try { data = await api("/api/markets"); }
  catch (err) { mount.replaceChildren(errorState(err, () => markets(mount))); return; }

  const board = data.board || {};
  mount.replaceChildren(frag(
    statRow(
      stat("Priced contracts", String(board.contracts_priced ?? data.count)),
      stat("Events read", String(board.events_seen ?? 0),
           `${board.events_out_of_slate ?? 0} in later weeks`),
      stat("Unparsed", String(board.contracts_unparsed ?? 0),
           board.contracts_unparsed ? "contract wording not recognised" : "every contract parsed"),
      stat("Unmapped events", String(board.events_unmapped ?? 0),
           board.events_unmapped ? "could not match to a scheduled game" : "all mapped"),
    ),
    board.unmapped_examples?.length
      ? notice(`<strong>Unmapped:</strong> ${board.unmapped_examples.join("; ")}`, "warn")
      : null,
    panel("Kalshi series covered", { sub: `${data.series.length} series` , flush: true },
      table([
        { label: "Series", render: (s) => el("span", { class: "mono-sm", text: s.ticker }) },
        { label: "Market", key: "label" },
        { label: "Category", key: "category" },
        { label: "Type", render: (s) => s.market_type.replace(/_/g, " ") },
        { label: "Priced as", render: (s) => (s.reference_only
            ? badge("reference", "reference") : badge("actionable", "medium")) },
      ], data.series)),
    panel("Live board", { sub: `${data.markets.length} contracts`, flush: true },
      marketTable(data.markets)),
  ));
}

