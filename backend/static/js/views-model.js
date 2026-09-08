/* Strategies, Model Lab, Performance and Backtests — the transparency half of the app. */

import {
  api, el, frag, panel, stat, statRow, badge, table, loading, emptyState, errorState,
  notice, disclosure, pct, signedPct, num, money, evClass, get, relativeTime,
} from "./core.js?v=2.0.7";
import { equityChart, calibrationChart, rankBars } from "./charts.js?v=2.0.7";

/* ----------------------------------------------------------------- strategies */

export async function strategies(mount) {
  mount.replaceChildren(loading(6));
  let data;
  try { data = await api("/api/strategies"); }
  catch (err) { mount.replaceChildren(errorState(err, () => strategies(mount))); return; }

  const weights = data.ensemble;
  mount.replaceChildren(frag(
    notice("Each strategy is scored separately and keeps its own track record. A model can be "
         + "genuinely good at totals and useless at spreads, and averaging those into one "
         + "accuracy number hides exactly the thing you need to know."),

    panel("Ensemble weights", { sub: weights.note, flush: true },
      table([
        { label: "Strategy", key: "strategy" },
        { label: "Prior", num: true, render: (r) => num(r.prior_weight, 2) },
        { label: "Performance ×", num: true, render: (r) => num(r.performance_multiplier, 2) },
        { label: "Effective", num: true, render: (r) => num(r.effective_weight, 2) },
        { label: "Record", render: (r) => (r.record
            ? `${r.record.n} settled${r.record.weighted ? "" : " (not yet weighted)"}`
            : "no settled predictions") },
      ], weights.weights)),

    ...data.strategies.map((s) => panel(s.name, {
      sub: s.market_types.map((m) => m.replace(/_/g, " ")).join(", "),
    },
      el("div", { class: "prose", text: s.methodology }),
      el("hr", { class: "rule" }),
      s.record.n
        ? frag(
            statRow(
              stat("Settled", String(s.record.n)),
              stat("Hit rate", pct(s.record.hit_rate)),
              stat("Brier", num(s.record.brier, 4), `market ${num(s.record.brier_market, 4)}`),
              stat("Log loss", num(s.record.log_loss, 4), `market ${num(s.record.log_loss_market, 4)}`),
              stat("ROI", signedPct(get(s.record, "roi.roi", 0)), "flat stakes",
                   evClass(get(s.record, "roi.roi", 0))),
            ),
            s.record.meaningful ? null : notice(s.record.note, "warn"))
        : notice(`No settled predictions for this strategy yet. ${s.record.note || ""}`),
      disclosure("Inputs and limitations",
        el("div", { class: "prose" },
          el("strong", { text: "Inputs: " }), s.inputs.join("; "), ".",
          el("br"), el("br"),
          el("strong", { text: "Limitations: " }), s.limitations)),
    )),
  ));
}

/* ------------------------------------------------------------------ model lab */

export async function modelLab(mount) {
  mount.replaceChildren(loading(8));
  let data;
  try { data = await api("/api/model"); }
  catch (err) { mount.replaceChildren(errorState(err, () => modelLab(mount))); return; }

  if (!data.artifact) {
    mount.replaceChildren(emptyState("Model still calibrating",
      "The game model is being fitted in the background. This takes under a minute on first "
    + "start; refresh shortly."));
    return;
  }

  const a = data.artifact;
  const r = data.ratings;

  mount.replaceChildren(frag(
    statRow(
      stat("Fitted on", `${a.sample_games}`, `games from ${a.seasons_fitted.join(", ")}`),
      stat("Margin RMSE", num(a.margin.rmse, 2), `R² ${num(a.margin.r2, 3)}`),
      stat("Total RMSE", num(a.total.rmse, 2), `R² ${num(a.total.r2, 3)}`),
      stat("Margin sigma", num(a.margin.sigma, 2), "points of outcome spread"),
      stat("Ratings as of", `Wk ${r.as_of.week}`, `${r.effective_games_per_team} eff. games/team`),
      stat("Rating confidence", pct(r.confidence, 0), r.seasons_used.join(", ")),
    ),

    panel("Pipeline", { sub: "raw data through to recommendations", flush: true },
      table([
        { label: "Stage", key: "stage" },
        { label: "What happens", key: "detail" },
      ], data.pipeline)),

    el("div", { class: "grid cols-2" },
      panel("Margin model coefficients", {
        sub: "shrunk by how well the data supports them", flush: true },
        table([
          { label: "Feature", render: (d) => d.feature.replace(/_/g, " ") },
          { label: "Estimate", num: true, render: (d) => num(d.estimate, 3) },
          { label: "Std err", num: true, render: (d) => num(d.std_error, 3) },
          { label: "t", num: true, render: (d) => num(d.t_stat, 2) },
          { label: "Shipped", num: true, render: (d) => num(d.shipped, 3) },
          { label: "", render: (d) => (d.supported ? badge("supported", "high") : badge("weak", "reference")) },
        ], a.margin_diagnostics || [])),

      panel("Total model coefficients", { flush: true },
        table([
          { label: "Feature", render: (d) => d.feature.replace(/_/g, " ") },
          { label: "Estimate", num: true, render: (d) => num(d.estimate, 3) },
          { label: "Std err", num: true, render: (d) => num(d.std_error, 3) },
          { label: "t", num: true, render: (d) => num(d.t_stat, 2) },
          { label: "Shipped", num: true, render: (d) => num(d.shipped, 3) },
          { label: "", render: (d) => (d.supported ? badge("supported", "high") : badge("weak", "reference")) },
        ], a.total_diagnostics || []))),

    notice("Coefficients marked <strong>weak</strong> are shrunk toward zero in proportion to "
         + "their t-statistic, so a factor the data cannot distinguish from noise cannot move "
         + "a projection. The intercept is never shrunk — where its shipped value differs "
         + "from its estimate, that is the re-centering that keeps predictions unbiased after "
         + "the other coefficients were shrunk."),

    panel("Key numbers", { sub: `fitted from ${a.key_numbers.games} historical games with closing lines` },
      el("div", { class: "prose" },
        "How much more often each exact outcome lands than a smooth model predicts, at the "
      + "numbers football actually clusters on. These multipliers are why a −2.5 and a −3.5 "
      + "are priced differently."),
      el("div", { class: "grid cols-2", style: "margin-top:12px" },
        el("div", {},
          el("div", { class: "stat-label", text: "Margin — multiplier vs a smooth model" }),
          // The bar encodes how far above (or below) 1.0 the multiplier sits; the printed
          // value is the multiplier itself, so "2.52x" cannot be misread as 1.52x.
          rankBars((a.key_numbers.margin_top || []).map(([k, v]) =>
            ({ label: String(k), value: v - 1, multiplier: v })),
            { format: (r) => `${num(r.multiplier, 2)}\u00d7` })),
        el("div", {},
          el("div", { class: "stat-label", text: "Total — multiplier vs a smooth model" }),
          rankBars((a.key_numbers.total_top || []).map(([k, v]) =>
            ({ label: String(k), value: v - 1, multiplier: v })),
            { format: (r) => `${num(r.multiplier, 2)}\u00d7` })))),

    panel("Power ratings", { sub: `net EPA per play, opponent-adjusted, as of week ${r.as_of.week}`, flush: true },
      table([
        { label: "#", num: true, key: "rank" },
        { label: "Team", key: "team" },
        { label: "Net", num: true, render: (t) => num(t.net, 4) },
        { label: "Offense", num: true, render: (t) => num(t.offense, 4) },
        { label: "Defense", num: true, render: (t) => num(t.defense, 4) },
        { label: "Sample", num: true, render: (t) => num(t.games, 1) },
      ], r.power)),

    panel("Projected points ratings", { sub: "expected points scored and suppressed", flush: true },
      table([
        { label: "#", num: true, key: "rank" },
        { label: "Team", key: "team" },
        { label: "Net", num: true, render: (t) => num(t.net, 2) },
        { label: "Offense", num: true, render: (t) => num(t.offense, 2) },
        { label: "Defense", num: true, render: (t) => num(t.defense, 2) },
      ], r.points)),
  ));
}

/* ---------------------------------------------------------------- performance */

export async function performance(mount) {
  mount.replaceChildren(loading(6));
  let data;
  try { data = await api("/api/performance"); }
  catch (err) { mount.replaceChildren(errorState(err, () => performance(mount))); return; }

  const o = data.overall;
  mount.replaceChildren(frag(
    notice(`<strong>How this page is kept honest.</strong> ${data.integrity}`),

    o.n
      ? frag(
          statRow(
            stat("Settled", String(o.n), `${data.pending_predictions} awaiting results`),
            stat("Hit rate", pct(o.hit_rate), `avg predicted ${pct(o.average_predicted)}`),
            stat("Brier", num(o.brier, 4), `market ${num(o.brier_market, 4)}`,
                 o.brier_market && o.brier < o.brier_market ? "pos" : "neg"),
            stat("Log loss", num(o.log_loss, 4), `market ${num(o.log_loss_market, 4)}`),
            stat("Calibration error", num(o.calibration_error, 4), "lower is better"),
            stat("ROI", signedPct(get(o, "roi.roi", 0)), "flat $1 stakes",
                 evClass(get(o, "roi.roi", 0))),
          ),
          o.meaningful ? null : notice(o.note, "warn"),

          el("div", { class: "grid cols-2" },
            panel("Equity curve", { sub: "cumulative profit at flat stakes" },
              equityChart(data.equity_curve)),
            panel("Calibration", { sub: "predicted vs observed" },
              calibrationChart(o.calibration))),

          o.clv
            ? panel("Closing line value", {
                sub: "did the market move toward us after we predicted?" },
                statRow(
                  stat("Mean CLV", signedPct(o.clv.mean_clv, 2)),
                  stat("Positive rate", pct(o.clv.positive_rate)),
                  stat("Sample", String(o.clv.n))),
                el("div", { class: "prose", style: "margin-top:10px" },
                  "Positive closing-line value is the fastest-converging evidence that a model "
                + "is finding real information — it becomes readable long before enough games "
                + "settle to trust a win rate."))
            : null,

          o.expected_vs_actual
            ? panel("Claimed edge vs realised", {},
                statRow(
                  stat("Claimed EV", signedPct(o.expected_vs_actual.expected_ev_per_dollar)),
                  stat("Actual ROI", signedPct(o.expected_vs_actual.actual_roi),
                       "", evClass(o.expected_vs_actual.actual_roi)),
                  stat("Gap", signedPct(o.expected_vs_actual.gap),
                       "negative means the edge did not show up",
                       evClass(o.expected_vs_actual.gap))))
            : null,

          panel("By strategy", { flush: true }, breakdownTable(data.by_strategy)),
          panel("By market", { flush: true }, breakdownTable(data.by_market)),
          panel("By confidence", {
            sub: "a well-behaved model wins more often when it says it is confident", flush: true },
            breakdownTable(data.by_confidence)),
        )
      : emptyState("No settled predictions yet",
          data.empty_reason
          || "Predictions are recorded before kickoff and graded once games finish."),

    panel("Database", { sub: "what has been recorded", flush: true },
      table([
        { label: "Table", render: (r) => r[0].replace(/_/g, " ") },
        { label: "Rows", num: true, render: (r) => String(r[1]) },
      ], Object.entries(data.counts))),
  ));
}

function breakdownTable(rows) {
  if (!rows?.length) return emptyState("Nothing to break down", "No settled predictions yet.");
  return table([
    { label: "Group", key: "label" },
    { label: "N", num: true, key: "n" },
    { label: "Hit rate", num: true, render: (r) => pct(r.hit_rate) },
    { label: "Predicted", num: true, render: (r) => pct(r.average_predicted) },
    { label: "Brier", num: true, render: (r) => num(r.brier, 4) },
    { label: "vs market", num: true, cls: (r) =>
        (r.brier_market && r.brier < r.brier_market ? "pos" : "neg"),
      render: (r) => (r.brier_market ? num(r.brier - r.brier_market, 4) : "—") },
    { label: "ROI", num: true, cls: (r) => evClass(get(r, "roi.roi", 0)),
      render: (r) => signedPct(get(r, "roi.roi", null)) },
    { label: "", render: (r) => (r.meaningful ? "" : badge("small sample", "warn")) },
  ], rows);
}

/* ------------------------------------------------------------------ backtests */

export async function backtests(mount) {
  const body = el("div", {}, loading(6));
  const seasonInput = el("input", { type: "text", value: "", id: "btSeasons",
                                    placeholder: "e.g. 2023,2024,2025", style: "width:170px" });
  const weightInput = el("input", { type: "number", step: "0.05", min: "0", max: "1",
                                    id: "btWeight", placeholder: "auto", style: "width:90px" });

  const run = async (payload) => {
    body.replaceChildren(loading(6));
    try {
      const data = payload
        ? await api("/api/backtest", { method: "POST", body: payload })
        : await api("/api/backtest/default");
      body.replaceChildren(renderBacktest(data));
    } catch (err) {
      body.replaceChildren(errorState(err, () => run(payload)));
    }
  };

  mount.replaceChildren(frag(
    el("div", { class: "controls" },
      el("label", { class: "field" }, "Seasons", seasonInput),
      el("label", { class: "field" }, "Model weight", weightInput),
      el("button", { class: "btn primary", text: "Run backtest", onClick: () => {
        const seasons = seasonInput.value.split(/[,\s]+/).map(Number).filter((n) => n > 1998);
        const weight = parseFloat(weightInput.value);
        run(seasons.length
          ? { seasons, start_week: 1, blend_weight: Number.isNaN(weight) ? null : weight }
          : null);
      } }),
      el("span", { class: "mono-sm", text: "leave blank for the standing three-season test" })),
    body,
  ));
  run(null);
}

function renderBacktest(data) {
  const o = data.overall;
  const v = data.value_only;
  const verdict = data.verdict || {};
  return frag(
    notice(`<strong>Verdict.</strong> ${verdict.summary || ""} ${verdict.what_it_means || ""}`,
           verdict.beats_market ? "" : "warn"),

    statRow(
      stat("Predictions", String(data.predictions), `${data.seasons.join(", ")}`),
      stat("Brier", num(o.brier, 4), `market ${num(o.brier_market, 4)}`,
           o.brier < o.brier_market ? "pos" : "neg"),
      stat("Log loss", num(o.log_loss, 4), `market ${num(o.log_loss_market, 4)}`),
      stat("Calibration error", num(o.calibration_error, 4), "lower is better"),
      stat("Hit rate", pct(o.hit_rate), `avg predicted ${pct(o.average_predicted)}`),
      stat("Runtime", `${num(data.elapsed_seconds, 1)}s`),
    ),

    el("div", { class: "grid cols-2" },
      panel("Calibration", { sub: "the most important chart on this page" },
        calibrationChart(o.calibration),
        el("div", { class: "prose", style: "margin-top:10px" },
          "If the model says 60% and 60% of those happen, the dots sit on the line. Distance "
        + "from the line is overconfidence or underconfidence — and it is fixable, whereas "
        + "being wrong about which side to take is not.")),

      panel("Bets we would actually have made", {
        sub: `${pct(v.selection_rate, 1)} of predictions cleared the value gate` },
        v.n
          ? statRow(
              stat("Bets", String(v.n)),
              stat("Hit rate", pct(v.hit_rate)),
              stat("ROI", signedPct(get(v, "roi.roi", 0)), "flat stakes",
                   evClass(get(v, "roi.roi", 0))),
              stat("Brier", num(v.brier, 4), `market ${num(v.brier_market, 4)}`))
          : emptyState("Nothing cleared the gate",
              "At this model weight the value gate selected no bets across the whole period."))),

    panel("By market", { flush: true }, breakdownTable(data.by_market)),
    panel("By confidence", { flush: true }, breakdownTable(data.by_confidence)),
    panel("By season", { flush: true }, breakdownTable(data.by_season)),

    panel("Method", {},
      el("div", { class: "prose" },
        el("strong", { text: "Look-ahead guarantee. " }), data.look_ahead_guarantee,
        el("br"), el("br"),
        el("strong", { text: "Benchmark. " }), data.benchmark,
        el("br"), el("br"),
        el("strong", { text: "Limitations. " }), data.limitations)),
  );
}
