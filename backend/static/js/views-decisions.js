/* Overview, My Bets and Performance.
 *
 * The organising principle: each view answers exactly one question, and refuses to answer
 * the others. Overview answers "what should I do?". My Bets answers "what do I hold and
 * how is it doing?". Performance answers "has any of this ever worked?".
 *
 * The old dashboard mixed all three, which meant the most important fact in the system —
 * that the model does not beat closing lines — was three clicks away from the page that
 * recommended bets.
 */

import {
  api, badge, cents, disclosure, el, emptyState, errorState, evClass, loading,
  money, notice, num, panel, pct, relativeTime, setChildren, signedPct, stat, statRow,
} from "./core.js?v=3";
import {
  healthPanel, modeBadge, openBetSlip, positionRow, recommendationCard, stateBadge,
} from "./ui-decisions.js?v=3";

/* ================================================================= OVERVIEW */

export async function overview(mount, { navigate, session }) {
  mount.replaceChildren(loading(4));
  let opportunities, health, portfolio = null;
  try {
    [opportunities, health] = await Promise.all([
      api("/api/opportunities?limit=8"),
      api("/api/health"),
    ]);
    if (session.user) {
      try { portfolio = await api("/api/portfolio"); } catch { portfolio = null; }
    }
  } catch (err) {
    return setChildren(mount, errorState(err, () => overview(mount, { navigate, session })));
  }

  const out = [];

  // The honesty banner goes ABOVE the recommendations, not below them. A person who reads
  // only the first screen should still learn that the edge is unproven.
  out.push(notice(
    "<strong>The model does not beat closing sportsbook lines.</strong> On 256 held-out "
    + "2025 games the blend is statistically indistinguishable from the market and the "
    + "model alone is measurably worse. Treat every edge below as unproven and size "
    + "accordingly. See Performance for the full evaluation.", "warn"));

  const live = portfolio?.live, paper = portfolio?.paper;
  out.push(statRow(
    stat("Week", `${health.week ?? "—"}`, `season ${health.season}`),
    stat("Qualifying bets", String(opportunities.count),
         opportunities.count ? "cleared the value gate" : "none today"),
    stat("Open exposure",
         money((live?.open_exposure || 0) + (paper?.open_exposure || 0)),
         live?.open_exposure ? `${money(live.open_exposure)} live` : "paper only"),
    stat("Realised P&L", money(live?.realised_pnl ?? 0), "live money only",
         evClass(live?.realised_pnl)),
  ));

  /* -------------------------------------------------- qualifying opportunities */
  const recs = opportunities.opportunities || [];
  out.push(panel("This week's qualifying bets", {
    sub: opportunities.ranking_basis,
  }, recs.length
    ? el("div", {}, ...recs.map((r) => recommendationCard(r, {
        onSelect: (rec) => {
          if (!session.user) return alert("Sign in to place or record a bet.");
          openBetSlip(rec, { mode: "paper", onPlaced: () => navigate("#/bets") });
        },
      })))
    : emptyState("No qualifying bets", opportunities.empty_reason
        || "Nothing on this board clears the value gate. That is a normal result.")));

  /* ------------------------------------------------------------- recent activity */
  if (portfolio) {
    const recent = [...(live?.open_positions || []), ...(paper?.open_positions || [])]
      .sort((a, b) => (b.created_at || 0) - (a.created_at || 0)).slice(0, 5);
    out.push(panel("Recent activity", { sub: `${recent.length} most recent positions` },
      recent.length
        ? el("div", {}, ...recent.map((p) => positionRow(p)))
        : emptyState("Nothing yet", "Positions you take will appear here.")));
  }

  /* ----------------------------------------------------------------- data health */
  const sources = Object.entries(health.data_sources || {}).map(([name, v]) => ({
    source: name,
    status: v.configured ? (v.schedule_loaded === false ? "stale" : "fresh") : "unknown",
    age_text: v.configured ? "configured" : "not configured",
  }));
  out.push(panel("Data health", { sub: "what the model can and cannot see" },
    healthPanel(sources),
    el("p", { class: "muted", style: "margin-top:10px;font-size:12px",
      text: health.model.calibrated
        ? `Model calibrated on ${health.model.sample_games} games from seasons `
          + `${(health.model.seasons_fitted || []).join(", ")}.`
        : "The game model has not been calibrated yet; projections are unavailable." })));

  setChildren(mount, ...out);
}

/* ================================================================== MY BETS */

export async function myBets(mount, { navigate, session }) {
  if (!session.user) {
    return setChildren(mount, emptyState("Sign in to track bets",
      "Positions, fills and P&L are tied to an account."));
  }
  mount.replaceChildren(loading(4));
  let portfolio;
  try {
    portfolio = await api("/api/portfolio");
  } catch (err) {
    return setChildren(mount, errorState(err, () => myBets(mount, { navigate, session })));
  }

  const out = [];
  const reload = () => myBets(mount, { navigate, session });

  async function closePosition(position, held) {
    const qty = prompt(
      `Close how many of the ${held} contracts still held?\n\n`
      + "The remainder stays open. Leave as-is to close everything.", String(held));
    if (qty === null) return;
    const contracts = Math.max(1, Math.min(Number(qty) || held, held));
    try {
      const result = await api("/api/positions/close", {
        method: "POST",
        body: { position_id: position.id, contracts },
      });
      alert(`${result.contracts_closed} contract(s) closed at `
            + `${cents(result.exit_price)}.\n${result.basis}\n\n`
            + `${result.remaining_open} still held.`);
      reload();
    } catch (err) {
      alert(`Could not close: ${err.message}`);
    }
  }

  /* Paper and live are rendered as two separate ledgers that are never summed. */
  for (const key of ["live", "paper"]) {
    const book = portfolio[key];
    if (!book) continue;
    const isLive = key === "live";
    const open = book.open_positions || [];
    const closed = book.closed_positions || [];
    if (!open.length && !closed.length) continue;

    out.push(panel(isLive ? "Live positions" : "Paper positions", {
      sub: isLive ? "real money on Kalshi" : "simulated against the real order book",
      actions: modeBadge(key),
    },
      statRow(
        stat("Open", String(book.open_count), "positions held"),
        stat("Exposure", money(book.open_exposure), "at entry price"),
        stat("Realised", money(book.realised_pnl), "after fees", evClass(book.realised_pnl)),
        stat("Fees paid", money(book.fees_paid), "entry + exit")),
      open.length
        ? el("div", { style: "margin-top:10px" },
            ...open.map((p) => positionRow(p, { onClose: closePosition })))
        : el("p", { class: "muted", style: "margin-top:10px",
                    text: "Nothing currently held." }),
      closed.length
        ? disclosure(`${closed.length} finished position(s)`,
            el("div", {}, ...closed.map((p) => positionRow(p))))
        : null));
  }

  if (!out.length) {
    out.push(emptyState("No bets yet",
      "Recommendations you act on will appear here with their real fills and P&L."));
  }

  /* --------------------------------------------------------------- reconciliation */
  const exec = portfolio.execution || {};
  out.push(panel("Reconciliation", { sub: "the exchange is the source of truth" },
    exec.reconciliation_supported
      ? el("div", {},
          el("p", { class: "muted", text:
            "Confirmed fills at Kalshi are authoritative. This app's record is a claim "
            + "until the exchange agrees with it." }),
          el("button", { class: "btn sm", text: "Reconcile now", onClick: async (e) => {
            e.target.disabled = true; e.target.textContent = "Checking…";
            try {
              const r = await api("/api/positions/reconcile", { method: "POST" });
              alert(`${r.reconciled} position(s) checked.\n`
                    + `${(r.mismatches || []).length} mismatch(es).\n\n${r.note}`);
            } catch (err) { alert(err.message); }
            reload();
          } }))
      : notice("Kalshi credentials are not configured, so live positions cannot be "
             + "reconciled against the exchange. Everything shown here is this app's "
             + "own record.", "warn")));

  out.push(panel("What the statuses mean", {},
    el("div", {}, ...Object.entries(portfolio.status_legend || {}).map(([k, v]) =>
      el("div", { class: "health-row" },
        stateBadge(k), el("span", { class: "name", text: v }))))));

  out.push(notice(portfolio.separation_note, ""));
  setChildren(mount, ...out);
}

/* ============================================================== PERFORMANCE */

function baselineTable(baselines, marketKey = "market") {
  const rows = baselines.map((b) => el("tr", { class: b.is_reference ? "reference" : "" },
    el("td", {}, el("strong", { text: b.name })),
    el("td", { class: "num", text: b.brier == null ? "—" : num(b.brier, 5) },
      b.brier_se ? el("div", { class: "se", text: `± ${num(b.brier_se, 5)}` }) : null),
    el("td", { class: "num", text: b.log_loss == null ? "—" : num(b.log_loss, 5) }),
    el("td", { class: "num", text: b.calibration_error == null ? "—"
                                    : pct(b.calibration_error, 2) }),
    el("td", { class: "num", text: String(b.games || 0) }),
    el("td", { class: "num", text: String(b.observations || 0) })));

  return el("div", { class: "table-scroll" },
    el("table", { class: "metric-table" },
      el("thead", {}, el("tr", {},
        el("th", { text: "Model" }),
        el("th", { class: "num", text: "Brier" }),
        el("th", { class: "num", text: "Log loss" }),
        el("th", { class: "num", text: "Calib. error" }),
        el("th", { class: "num", text: "Games" }),
        el("th", { class: "num", text: "Contracts" }))),
      el("tbody", {}, ...rows)));
}

function calibrationChart(table) {
  const populated = (table || []).filter((b) => b.n);
  if (!populated.length) return el("p", { class: "muted", text: "No calibration data." });
  return el("div", {},
    el("div", { class: "calib" }, ...populated.map((b) => el("div", { class: "calib-bin" },
      el("div", { class: "calib-bar observed",
                  style: `height:${Math.round((b.observed || 0) * 88)}px`,
                  title: `observed ${pct(b.observed)} in ${b.n} forecasts` }),
      el("div", { class: "calib-bar predicted",
                  style: `height:${Math.round((b.predicted || 0) * 88)}px`,
                  title: `predicted ${pct(b.predicted)}` })))),
    el("div", { class: "calib-axis" },
      ...populated.map((b) => el("span", { text: b.bin }))),
    el("p", { class: "muted", style: "font-size:11px;margin-top:8px",
      text: "Blue is what actually happened; grey is what the model predicted. Equal "
          + "heights mean the model is calibrated." }));
}

export async function performance(mount, { navigate, session }) {
  mount.replaceChildren(loading(6));
  let live, evaluation = null, evalError = null;
  try {
    live = await api("/api/performance");
  } catch (err) {
    return setChildren(mount, errorState(err, () => performance(mount, { navigate, session })));
  }
  try {
    evaluation = await api("/api/evaluation");
  } catch (err) {
    evalError = err;
  }

  const out = [];

  /* ------------------------------------------------- held-out evaluation first */
  if (evaluation) {
    const verdict = evaluation.verdict || {};
    const test = (evaluation.periods || {}).test || {};
    out.push(notice(
      `<strong>${verdict.promotable ? "A model beats the market." : "Nothing beats the market."}</strong> `
      + verdict.summary, verdict.promotable ? "ok" : "warn"));

    out.push(panel("Held-out evaluation", {
      sub: `${test.games || 0} games in season ${(test.seasons || []).join(", ")} — never `
         + "used to fit or tune anything",
    },
      baselineTable(test.baselines || []),
      el("p", { class: "muted", style: "margin-top:10px;font-size:12px",
        text: "Intervals are computed between GAMES, not between contracts. Six contracts "
            + "on one game resolve off one scoreline and are one unit of evidence." }),
      disclosure("Look-ahead check",
        el("p", { text: (evaluation.look_ahead || {}).statement }),
        el("div", { class: "table-scroll", style: "margin-top:8px" },
          el("table", { class: "metric-table" },
            el("thead", {}, el("tr", {},
              el("th", { text: "Scored season" }),
              el("th", { text: "Fitted only on" }))),
            el("tbody", {}, ...Object.entries(
              (evaluation.look_ahead || {}).fits_by_scored_season || {}).map(
                ([season, fitted]) => el("tr", {},
                  el("td", { text: season }),
                  el("td", { class: "num", text: fitted.join(", ") })))))))));

    /* by market */
    const byMarket = evaluation.by_market || {};
    if (Object.keys(byMarket).length) {
      out.push(panel("By market", { sub: "held-out games only" },
        ...Object.entries(byMarket).map(([market, d]) => el("div", { style: "margin-bottom:14px" },
          el("h4", { style: "margin:0 0 6px;text-transform:capitalize",
                     text: `${market} — ${d.games} games` }),
          baselineTable(d.baselines || [])))));
    }

    /* calibration of the published blend */
    const blend = (test.baselines || []).find((b) => b.key === "blend");
    if (blend) {
      out.push(panel("Calibration of the published probability", {
        sub: `expected calibration error ${pct(blend.calibration_error || 0, 2)}`,
      }, calibrationChart(blend.calibration)));
    }

    /* ablations */
    const ablations = evaluation.ablations || {};
    if (Object.keys(ablations).length) {
      out.push(panel("What each component is worth", {
        sub: "measured by removing it, not assumed",
      }, el("div", { class: "table-scroll" }, el("table", { class: "metric-table" },
        el("thead", {}, el("tr", {},
          el("th", { text: "Component" }),
          el("th", { class: "num", text: "Brier when removed" }),
          el("th", { text: "Verdict" }))),
        el("tbody", {}, ...Object.values(ablations).map((a) => el("tr", {},
          el("td", { text: a.name }),
          // A Brier delta is a score difference, not a percentage. Formatting 0.00921 as
      // "+0.921%" invites reading it as a rate.
      el("td", { class: "num", text: a.measured
            ? (a.brier_change_when_removed > 0 ? "+" : "")
              + num(a.brier_change_when_removed, 5) : "—" },
         a.standard_error
           ? el("div", { class: "se", text: `± ${num(a.standard_error, 5)}` }) : null),
          el("td", { class: "muted", text: a.verdict || a.note || "—" }))))))));
    }

    /* returns */
    const returns = evaluation.returns || {};
    if (Object.keys(returns).length) {
      out.push(panel("Flat-stake returns after fees", {
        sub: "at closing sportsbook prices, with Kalshi's fee applied",
      }, el("div", { class: "table-scroll" }, el("table", { class: "metric-table" },
        el("thead", {}, el("tr", {},
          el("th", { text: "Model" }), el("th", { class: "num", text: "Bets" }),
          el("th", { class: "num", text: "Staked" }), el("th", { class: "num", text: "P&L" }),
          el("th", { class: "num", text: "ROI" }))),
        el("tbody", {}, ...Object.entries(returns).map(([k, r]) => el("tr", {},
          el("td", { text: k }),
          el("td", { class: "num", text: String(r.bets || 0) }),
          el("td", { class: "num", text: money(r.staked) }),
          el("td", { class: "num " + evClass(r.pnl), text: money(r.pnl) }),
          el("td", { class: "num " + evClass(r.roi),
                     text: r.roi == null ? "—" : signedPct(r.roi) },
             r.roi_se ? el("div", { class: "se", text: `± ${signedPct(r.roi_se)}` }) : null)
        )))))));
    }
  } else if (evalError) {
    out.push(notice(`The held-out evaluation could not be produced: ${evalError.message}`,
                    "warn"));
  }

  /* --------------------------------------------------------- live tracked record */
  const overall = live.overall || {};
  out.push(panel("Live tracked record", {
    sub: "predictions recorded before outcomes, never edited",
  },
    statRow(
      stat("Forecasts", String(overall.n || 0),
           `${overall.games || 0} games`),
      stat("Brier", overall.brier == null ? "—" : num(overall.brier, 4),
           overall.brier_se ? `± ${num(overall.brier_se, 4)}` : ""),
      stat("Market Brier", overall.brier_market == null ? "—"
           : num(overall.brier_market, 4), "same forecasts"),
      stat("Meaningful?", overall.meaningful ? "Yes" : "Not yet",
           `${overall.games || 0} of 30 games`, overall.meaningful ? "pos" : "warn")),
    overall.sample_note
      ? el("p", { class: "muted", style: "margin-top:10px;font-size:12px",
                  text: overall.sample_note })
      : null,
    el("p", { class: "muted", style: "margin-top:6px;font-size:12px",
              text: overall.note || "" })));

  out.push(panel("Limitations", { sub: "read these before quoting any number above" },
    el("ul", { style: "margin:0;padding-left:18px;line-height:1.6" },
      ...(evaluation?.limitations || [
        "The held-out evaluation could not be loaded.",
      ]).map((l) => el("li", { class: "muted", text: l })))));

  setChildren(mount, ...out);
}
