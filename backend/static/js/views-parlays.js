/* Parlays — with the two products kept visibly apart.
 *
 * The defect this view exists to correct: the board showed a payout multiple derived by
 * multiplying leg costs, and the buy button placed separate single contracts. Those are
 * different products with different payouts, and the number on screen belonged to the one
 * that was not being bought.
 *
 * So every combination now renders as a comparison, never a single figure:
 *
 *   BASKET OF SINGLES   buyable, additive payouts, partial wins pay. Has a buy button.
 *   KALSHI COMBINATION  buyable only when a real combo market exists and has an ask. The
 *                       price shown is the venue's, not ours.
 *   HYPOTHETICAL PARLAY analysis only. Drawn inside a hatched dashed box, labelled, and
 *                       structurally has no buy button — not a disabled one.
 */

import {
  api, badge, cents, disclosure, el, emptyState, errorState, evClass, loading,
  money, notice, num, panel, pct, setChildren, signedPct,
} from "./core.js?v=3";
import { modeBadge } from "./ui-decisions.js?v=4";

function legRow(leg) {
  return el("div", { class: "health-row" },
    el("span", { class: "name" },
      el("strong", { text: leg.label || leg.selection || leg.ticker }),
      el("div", { class: "muted", style: "font-size:11px",
                  text: leg.reasoning?.[0] || leg.market_type || "" })),
    el("span", { class: "age", text: `model ${pct(leg.model_prob)}` }),
    el("span", { class: "age", text: cents(leg.cost) }),
    badge(leg.confidence || "", leg.confidence === "high" ? "pos" : ""));
}

/** The side-by-side that stops one product's payout being read as the other's. */
function productComparison(parlay, { onBuyBasket, comboQuote } = {}) {
  const products = parlay.products || {};
  const basket = products.basket_of_singles;
  const hypothetical = products.hypothetical_parlay;
  const stake = parlay.reference_stake || 100;

  const panels = [];

  if (basket) {
    panels.push(el("div", {},
      el("h4", { text: "Basket of singles — buyable" }),
      el("div", { class: "big", text: money(basket.max_payout) }),
      el("div", { class: "muted", style: "font-size:11px",
                  text: `if all ${basket.leg_count} legs win, on ${money(basket.stake)} staked` }),
      el("div", { class: "note" },
        "Separate contracts. Each leg pays on its own, so a partial result still returns "
        + "money. Fees "),
      el("div", { class: "note", text: money(basket.fees) }),
      onBuyBasket
        ? el("button", { class: "btn sm primary", style: "margin-top:9px",
                         onClick: () => onBuyBasket(parlay, basket),
                         text: `Buy as ${basket.leg_count} singles` })
        : null));
  }

  if (hypothetical) {
    panels.push(el("div", { class: "hypothetical" },
      el("div", { class: "tag", text: "Hypothetical — not for sale" }),
      el("div", { class: "big", text: money(hypothetical.payout_multiple * stake) }),
      el("div", { class: "muted", style: "font-size:11px",
                  text: `if all legs win, on ${money(stake)} — all-or-nothing` }),
      el("div", { class: "note", text: hypothetical.note })));
  }

  const wrap = el("div", {}, el("div", { class: "compare" }, ...panels));

  if (comboQuote) {
    wrap.append(comboQuote.executable
      ? el("div", { class: "notice ok", style: "margin-top:10px" },
          el("strong", { text: "A real Kalshi combination exists for these legs" }),
          el("p", { style: "margin:6px 0 0",
            text: `${comboQuote.ticker} — quoted at ${cents(comboQuote.cost)}, `
                + `${money(comboQuote.depth_usd)} resting. This price is Kalshi's, not a `
                + "calculation." }))
      : el("div", { class: "notice", style: "margin-top:10px" },
          el("strong", { text: "No genuine combination quote" }),
          el("p", { style: "margin:6px 0 0", text: comboQuote.reason }),
          el("p", { style: "margin:6px 0 0", class: "muted",
                    text: comboQuote.alternative || "" })));
  }

  wrap.append(el("p", { class: "muted", style: "margin-top:8px;font-size:12px",
    text: (parlay.comparison || {}).difference_explained || "" }));
  return wrap;
}

function parlayCard(parlay, { onBuyBasket, onQuote } = {}) {
  const card = el("section", { class: "panel", style: "margin-bottom:14px" });

  card.append(el("div", { class: "panel-head" },
    el("h2", { text: `${parlay.leg_count} legs · ${pct(parlay.model_probability)} to win` }),
    el("span", { class: "sub",
      text: `${signedPct(parlay.estimated_edge)} edge vs market · `
          + `EV ${signedPct(parlay.ev_per_dollar)} after fees` }),
    el("div", { class: "spacer" }),
    badge(parlay.risk_rating, parlay.risk_rating === "moderate" ? "" : "warn")));

  const body = el("div", { class: "panel-body" });

  // Simulation uncertainty, shown where it matters: two parlays a point apart are not
  // meaningfully different if the interval is three points wide.
  if (parlay.probability_range) {
    body.append(el("p", { class: "muted", style: "font-size:12px;margin:0 0 10px",
      text: `Simulated win probability ${pct(parlay.model_probability)}, `
          + `95% interval ${pct(parlay.probability_range[0])} to `
          + `${pct(parlay.probability_range[1])}. Differences smaller than that are noise.` }));
  }

  body.append(el("div", { style: "margin-bottom:12px" },
    ...(parlay.legs || []).map(legRow)));

  body.append(productComparison(parlay, { onBuyBasket }));

  if (onQuote) {
    body.append(el("button", {
      class: "btn sm", style: "margin-top:10px",
      onClick: (e) => onQuote(parlay, e.target),
      text: "Check for a real Kalshi combination" }));
  }

  const explanation = parlay.explanation || [];
  const warnings = parlay.warnings || [];
  if (explanation.length || warnings.length) {
    body.append(disclosure("Why these legs, and what can go wrong",
      ...explanation.map((line) => el("p", { class: "muted", text: line })),
      ...warnings.map((line) => el("p", { class: "rec-missing", text: line }))));
  }

  card.append(body);
  return card;
}

export async function parlays(mount, { navigate, session }) {
  mount.replaceChildren(loading(5));
  let board;
  try {
    board = await api("/api/parlays");
  } catch (err) {
    return setChildren(mount, errorState(err, () => parlays(mount, { navigate, session })));
  }

  const out = [];

  out.push(notice(
    "<strong>Kalshi does sell real combination contracts</strong>, through multivariate "
    + "event collections. When one exists for a set of legs, the price below is Kalshi's "
    + "own quote. When one does not, the all-or-nothing figure is clearly marked "
    + "hypothetical and cannot be bought — what you can buy instead is a basket of "
    + "separate singles, which pays additively and far less.", ""));

  async function buyBasket(parlay, basket) {
    if (!session.user) return alert("Sign in to place or record a bet.");
    const stake = Number(prompt(
      `Stake for this basket of ${basket.leg_count} separate contracts?\n\n`
      + "The stake is split across the legs. Each leg pays on its own.", "20"));
    if (!stake || stake <= 0) return;
    try {
      const result = await api("/api/parlays/place", {
        method: "POST",
        body: {
          product: "basket_of_singles",
          mode: "paper",
          category: parlay.category,
          stake,
          combined_prob: parlay.model_probability,
          ev_per_dollar: parlay.ev_per_dollar,
          risk_rating: parlay.risk_rating,
          legs: (parlay.legs || []).map((l) => ({
            ticker: l.ticker, side: "yes", game_id: l.game_id,
            label: l.label, market_type: l.market_type, model_prob: l.model_prob,
          })),
        },
      });
      alert(`${result.filled} of ${result.requested} legs filled.\n\n${result.note}`);
      navigate("#/bets");
    } catch (err) {
      alert(`Could not place: ${err.message}`);
    }
  }

  async function checkQuote(parlay, button) {
    button.disabled = true;
    button.textContent = "Asking Kalshi…";
    try {
      const quote = await api("/api/parlays/quote", {
        method: "POST",
        body: {
          stake: 100,
          legs: (parlay.legs || []).map((l) => ({ ticker: l.ticker, side: "yes" })),
        },
      });
      button.replaceWith(quote.executable
        ? el("div", { class: "notice ok" },
            el("strong", { text: "Real combination available" }),
            el("p", { style: "margin:6px 0 0",
              text: `${quote.ticker} at ${cents(quote.cost)}, `
                  + `${money(quote.depth_usd)} resting.` }))
        : el("div", { class: "notice warn" },
            el("strong", { text: "No real combination for these legs" }),
            el("p", { style: "margin:6px 0 0", text: quote.reason }),
            el("p", { style: "margin:6px 0 0", class: "muted",
                      text: quote.alternative || "" })));
    } catch (err) {
      button.disabled = false;
      button.textContent = "Check for a real Kalshi combination";
      alert(err.message);
    }
  }

  const categories = Array.isArray(board) ? board : (board.categories || []);
  let any = false;
  for (const category of categories) {
    const list = category.parlays || [];
    if (!list.length) {
      out.push(panel(category.name || category.category, { sub: category.description },
        emptyState("Nothing here", category.note
          || "No combination at this risk level clears a positive expected return.")));
      continue;
    }
    any = true;
    out.push(el("h3", { style: "margin:18px 0 8px", text: category.name || category.category }));
    if (category.description) {
      out.push(el("p", { class: "muted", style: "margin:0 0 10px;font-size:12px",
                         text: category.description }));
    }
    out.push(...list.map((p) => parlayCard(p, { onBuyBasket: buyBasket,
                                                onQuote: checkQuote })));
  }

  if (!any && !categories.length) {
    out.push(emptyState("No parlays", "The board produced no combinations."));
  }

  setChildren(mount, ...out);
}
