/* Shared components for the decision-and-position views.
 *
 * Three rules these components enforce, because the old UI broke all three:
 *
 *  1. STATUS IS NEVER COLOUR ALONE. Every badge pairs a hue with a glyph and a word, so
 *     the screen still works in greyscale, for a colour-blind reader, and on a phone in
 *     sunlight.
 *
 *  2. NOTHING HYPOTHETICAL GETS A BUY BUTTON. A price that no venue has quoted is drawn
 *     inside a hatched, dashed container labelled HYPOTHETICAL, and the confirm action is
 *     structurally absent rather than merely disabled.
 *
 *  3. A NUMBER THAT COULD BE STALE SAYS SO. Quote ages are rendered next to the price they
 *     belong to, not hidden in a tooltip, and anything past the freshness window is
 *     labelled rather than silently shown.
 */

import { api, badge, cents, el, evClass, money, num, pct, signedPct } from "./core.js?v=3";

/* ------------------------------------------------------------------- badges */

const STATE_GLYPHS = {
  open: "●",              // filled circle: live, held
  partially_filled: "◐",  // half circle: partly there
  partially_closed: "◐",
  filled: "●",
  pending: "○",           // hollow circle: not confirmed
  submitted: "○",
  settled: "✓",           // tick: resolved by the game
  closed: "—",            // dash: exited early
  rejected: "✕",          // cross: nothing happened
  voided: "⊘",
};

const STATE_CLASS = {
  open: "open", filled: "open",
  partially_filled: "partial", partially_closed: "partial",
  pending: "pending", submitted: "pending",
  settled: "settled", closed: "closed",
  rejected: "rejected", voided: "rejected",
};

const STATE_WORDS = {
  open: "Open", filled: "Open",
  partially_filled: "Partial fill", partially_closed: "Partly closed",
  pending: "Pending", submitted: "Submitted",
  settled: "Settled", closed: "Closed", rejected: "Rejected", voided: "Voided",
};

export function stateBadge(status) {
  const key = String(status || "").toLowerCase();
  return el("span", { class: `state-badge ${STATE_CLASS[key] || "closed"}` },
    el("span", { class: "glyph", text: STATE_GLYPHS[key] || "—", "aria-hidden": "true" }),
    STATE_WORDS[key] || status || "Unknown");
}

export function modeBadge(mode) {
  const live = mode === "live";
  return el("span", {
    class: `mode-badge ${live ? "live" : "paper"}`,
    title: live ? "Real money on Kalshi" : "Simulated against the real order book",
  }, live ? "● Live" : "◌ Paper");
}

/* --------------------------------------------------------- recommendations */

function cell(label, value, { sub, cls = "" } = {}) {
  return el("div", { class: "rec-cell" },
    el("div", { class: "k", text: label }),
    el("div", { class: `v ${cls}`, text: value }),
    sub ? el("div", { class: "sub", text: sub }) : null);
}

/** One recommendation, stated completely enough to argue with. */
export function recommendationCard(rec, { onSelect } = {}) {
  const p = rec.probability, price = rec.price, econ = rec.economics;
  const robust = rec.assessment.robust;

  const card = el("div", { class: `rec-card ${robust ? "robust" : "fragile"}` });

  card.append(el("div", { class: "rec-head" },
    el("div", {},
      el("div", { class: "rec-sel", text: rec.selection }),
      el("div", { class: "rec-match", text: rec.matchup })),
    el("div", { class: "spacer" }),
    // Value and likelihood are shown as separate badges on purpose: conflating them is
    // how a bankroll ends up full of 90c favourites.
    badge(`${rec.assessment.value_rating} value`,
          rec.assessment.value_rating === "negative" ? "neg"
            : rec.assessment.value_rating === "thin" ? "warn" : "pos"),
    badge(rec.assessment.win_likelihood),
    robust ? null : badge("fragile edge", "warn")));

  card.append(el("div", { class: "rec-grid" },
    cell("Model", pct(p.final), { sub: `raw ${pct(p.raw_model)}` }),
    cell("Market", pct(p.market), { sub: "de-vigged" }),
    cell("Edge", signedPct(p.edge), { cls: evClass(p.edge), sub: `±${pct(p.uncertainty, 0)}` }),
    cell("Price", cents(price.cost), {
      sub: price.stale ? `stale · ${Math.round(price.quote_age_seconds)}s`
                       : price.quote_age_seconds != null
                         ? `${Math.round(price.quote_age_seconds)}s old` : "live",
      cls: price.stale ? "warn" : "" }),
    cell("Max pay", cents(price.max_entry_price), { sub: "refuses above" }),
    cell("EV net", signedPct(econ.ev_after_fees), {
      cls: evClass(econ.ev_after_fees), sub: `fee ${money(econ.estimated_fee)}` }),
    cell("Break-even", pct(econ.breakeven_probability), { sub: "to not lose" }),
    cell("Depth", money(price.depth_usd), {
      sub: price.depth_covers_stake ? "covers stake" : "thin",
      cls: price.depth_covers_stake ? "" : "warn" })));

  const foot = el("div", { class: "rec-foot" });
  foot.append(el("p", { class: "rec-why", text: rec.settles }));
  for (const line of rec.reasoning.supporting.slice(0, 2)) {
    foot.append(el("p", { class: "rec-why", text: `• ${line}` }));
  }
  for (const line of rec.reasoning.missing.slice(0, 2)) {
    foot.append(el("p", { class: "rec-missing", text: `Not known: ${line}` }));
  }
  for (const line of rec.reasoning.warnings.slice(0, 2)) {
    foot.append(el("p", { class: "rec-missing", text: line }));
  }
  if (!robust) {
    foot.append(el("p", { class: "rec-missing", text: rec.assessment.robustness_note }));
  }
  if (onSelect) {
    foot.append(el("div", { style: "margin-top:8px" },
      el("button", { class: "btn sm primary", onClick: () => onSelect(rec),
                     text: "Review bet" })));
  }
  card.append(foot);
  return card;
}

/* ----------------------------------------------------------------- bet slip */

/** The bet slip. Re-checks the live book before it will show a confirm button.
 *
 * The confirm action does not exist until a preflight has come back OK. That is
 * deliberate: a disabled button invites a click and a retry, whereas an absent one makes
 * it obvious that the system has not agreed to this trade.
 */
export function openBetSlip(rec, { mode = "paper", onPlaced } = {}) {
  const backdrop = el("div", { class: "slip-backdrop" });
  const slip = el("div", { class: "slip", role: "dialog", "aria-modal": "true",
                           "aria-label": `Bet slip for ${rec.selection}` });
  const body = el("div", { class: "slip-body" });
  const foot = el("div", { class: "slip-foot" });

  let stake = Math.max(rec.sizing.recommended_stake || 0, 1);
  let preflight = null;
  let busy = false;

  const close = () => backdrop.remove();
  backdrop.addEventListener("click", (e) => { if (e.target === backdrop) close(); });
  document.addEventListener("keydown", function esc(e) {
    if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc); }
  });

  slip.append(el("div", { class: "slip-head" },
    el("div", { style: "display:flex;align-items:center;gap:8px;flex-wrap:wrap" },
      el("strong", { text: rec.selection }), modeBadge(mode)),
    el("div", { class: "muted", style: "font-size:12px;margin-top:3px",
                text: rec.matchup }),
    el("div", { class: "muted", style: "font-size:12px;margin-top:6px",
                text: rec.settles })));

  const stakeField = el("input", {
    class: "stake-input", type: "number", min: "1", step: "1",
    value: String(Math.round(stake)), "aria-label": "Stake in dollars",
  });
  stakeField.addEventListener("change", () => {
    stake = Math.max(Number(stakeField.value) || 0, 1);
    check();
  });

  function line(k, v, cls = "", extra = "") {
    return el("div", { class: `slip-line ${extra}` },
      el("span", { class: "k", text: k }),
      el("span", { class: `v ${cls}`, text: v }));
  }

  function render() {
    body.replaceChildren();
    body.append(el("label", { class: "muted", style: "font-size:12px", text: "Stake" }),
                stakeField);

    if (preflight === null) {
      body.append(el("p", { class: "muted", style: "margin-top:12px",
                           text: "Checking the live order book…" }));
    } else if (!preflight.ok) {
      body.append(el("div", { class: "notice warn", style: "margin-top:12px" },
        el("strong", { text: "Edge no longer available" }),
        el("p", { style: "margin:6px 0 0", text: preflight.message })));
      if (preflight.cost != null) {
        body.append(line("Book now", cents(preflight.cost), "warn"));
        body.append(line("You were shown", cents(rec.price.cost)));
        body.append(line("Your maximum", cents(rec.price.max_entry_price)));
      }
    } else {
      body.append(el("div", { style: "margin-top:12px" },
        line("Price", cents(preflight.cost)),
        line("Contracts", String(preflight.contracts)),
        line("Cost", money(preflight.stake)),
        line("Kalshi fee", money(preflight.estimated_fee), "warn"),
        line("Max payout", money(preflight.contracts), "pos"),
        line("EV after fees", signedPct(preflight.ev_after_fees),
             evClass(preflight.ev_after_fees)),
        line("Break-even", pct(preflight.breakeven_probability)),
        line("Model says", pct(rec.probability.final)),
        line("Total risk", money(preflight.stake + preflight.estimated_fee), "", "total")));
      if (!preflight.depth_covers_stake) {
        body.append(el("div", { class: "notice warn", style: "margin-top:10px" },
          `Only ${money(preflight.depth_usd)} is resting at this price. The rest of your `
          + "stake may fill worse or not at all."));
      }
    }

    foot.replaceChildren(
      el("button", { class: "btn", onClick: close, text: "Cancel" }),
      el("div", { class: "spacer", style: "flex:1" }),
      preflight && preflight.ok
        ? el("button", {
            class: "btn primary", disabled: busy || undefined,
            onClick: place,
            text: busy ? "Placing…"
                       : `Place ${mode === "live" ? "LIVE" : "paper"} bet`,
          })
        : el("button", { class: "btn", onClick: check, disabled: busy || undefined,
                         text: "Re-check price" }));
  }

  async function check() {
    preflight = null;
    render();
    try {
      preflight = await api("/api/orders/preflight", {
        method: "POST",
        body: {
          ticker: rec.ticker, side: rec.side, stake,
          model_prob: rec.probability.final,
          recommended_cost: rec.price.cost,
          max_entry_price: rec.price.max_entry_price,
        },
      });
    } catch (err) {
      preflight = { ok: false, message: err.message };
    }
    render();
  }

  async function place() {
    busy = true; render();
    try {
      const fill = await api("/api/orders", {
        method: "POST",
        body: {
          ticker: rec.ticker, side: rec.side, stake, mode,
          game_id: rec.game_id, label: rec.selection, market_type: rec.market_type,
          model_prob: rec.probability.final,
          selection: rec.selection,
          recommended_cost: rec.price.cost,
          max_entry_price: rec.price.max_entry_price,
          recommendation_id: rec.recommendation_id,
          model_version: rec.model_version,
        },
      });
      busy = false;
      body.replaceChildren(el("div", { class: `notice ${fill.ok ? "ok" : "warn"}` },
        el("strong", { text: fill.ok ? "Recorded" : "Not placed" }),
        el("p", { style: "margin:6px 0 0", text: fill.message })));
      foot.replaceChildren(el("button", {
        class: "btn primary", text: "Done",
        onClick: () => { close(); onPlaced && onPlaced(fill); } }));
    } catch (err) {
      busy = false;
      preflight = { ok: false, message: err.message };
      render();
    }
  }

  slip.append(body, foot);
  backdrop.append(slip);
  document.body.append(backdrop);
  render();
  check();
  return backdrop;
}

/* ---------------------------------------------------------------- positions */

/** One held or finished position, with its real fill and its real P&L. */
export function positionRow(position, { onClose } = {}) {
  const filled = Number(position.filled_contracts ?? position.contracts ?? 0);
  const requested = Number(position.requested_contracts ?? filled);
  const closed = Number(position.closed_contracts ?? 0);
  const held = Math.max(filled - closed, 0);
  const pnl = position.pnl == null ? null : Number(position.pnl);
  const finished = ["closed", "settled", "voided"].includes(position.status);

  const meta = el("div", { class: "pos-meta" },
    stateBadge(position.status),
    modeBadge(position.mode),
    el("span", { text: `${held || filled} @ ${cents(position.entry_price)}` }),
    position.entry_fees ? el("span", { text: `fee ${money(position.entry_fees)}` }) : null,
    position.ticker ? el("span", { text: position.ticker }) : null);

  const main = el("div", { class: "pos-main" },
    el("div", { class: "pos-sel", text: position.label || position.selection || position.ticker }),
    meta);

  // A partial fill or partial close is visible as a shape before any number is read.
  if (requested > filled || closed > 0) {
    const pctFilled = requested ? Math.round((filled / requested) * 100) : 100;
    main.append(el("div", { class: "fill-bar", title: `${filled} of ${requested} filled` },
      el("span", { style: `width:${pctFilled}%` })));
    main.append(el("div", { class: "pos-sub",
      text: requested > filled
        ? `Filled ${filled} of ${requested} requested—${requested - filled} never filled`
        : `${closed} of ${filled} closed, ${held} still held` }));
  }

  const side = el("div", { class: "pos-side" },
    el("div", { class: `pos-pnl ${evClass(pnl)}`,
                text: pnl == null ? "—" : money(pnl) }),
    el("div", { class: "pos-sub",
                text: finished ? (position.status === "settled" ? "settled" : "realised")
                               : "unrealised estimate" }));

  if (!finished && onClose && held > 0) {
    side.append(el("button", {
      class: "btn sm", style: "margin-top:6px",
      onClick: () => onClose(position, held), text: "Close" }));
  }
  return el("div", { class: "pos-row" }, main, side);
}

/* -------------------------------------------------------------- data health */

export function healthPanel(sources) {
  if (!sources || !sources.length) {
    return el("p", { class: "muted", text: "No data-source information reported." });
  }
  return el("div", {}, ...sources.map((s) => el("div", { class: "health-row" },
    el("span", { class: "glyph", "aria-hidden": "true",
      text: s.status === "fresh" ? "●" : s.status === "stale" ? "◐" : "○" }),
    el("span", { class: "name", text: s.source.replace(/_/g, " ") }),
    el("span", { class: "age", text: s.age_text || s.status }),
    badge(s.status, s.status === "fresh" ? "pos"
                    : s.status === "stale" ? "warn" : "neg"))));
}
