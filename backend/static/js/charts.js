/* Inline SVG charts.
 *
 * Every chart here answers a question you would otherwise have to compute in your head:
 * is the model calibrated, where is this game's margin likely to land, has the price been
 * drifting, is the equity curve real or one lucky week. Nothing is drawn for decoration —
 * if a number reads better as a number, it stays a number.
 *
 * Written as raw SVG rather than a charting library so the page has no external
 * dependency, works offline, and inherits the theme through CSS variables.
 */

import { el, pct, num } from "./core.js?v=2.1.0";

const NS = "http://www.w3.org/2000/svg";

function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined) continue;
    node.setAttribute(k, String(v));
  }
  for (const c of children.flat()) if (c) node.append(c);
  return node;
}

function scale(domain, range) {
  const [d0, d1] = domain;
  const [r0, r1] = range;
  const span = d1 - d0 || 1;
  return (value) => r0 + ((value - d0) / span) * (r1 - r0);
}

/** Cumulative profit over time. The only honest way to look at a betting record. */
export function equityChart(points, { height = 180 } = {}) {
  if (!points || points.length < 2) {
    return el("div", { class: "state" },
      el("p", { text: "An equity curve needs at least two settled predictions." }));
  }
  const width = 640;
  const pad = { top: 12, right: 12, bottom: 22, left: 42 };
  const values = points.map((p) => p.cumulative);
  const min = Math.min(0, ...values);
  const max = Math.max(0, ...values);
  const x = scale([0, points.length - 1], [pad.left, width - pad.right]);
  const y = scale([min, max], [height - pad.bottom, pad.top]);

  const path = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.cumulative).toFixed(1)}`).join(" ");
  const last = values[values.length - 1];

  const ticks = [min, (min + max) / 2, max];
  return el("div", {},
    svg("svg", { class: "chart", viewBox: `0 0 ${width} ${height}`, role: "img",
                 "aria-label": `Cumulative profit, currently ${num(last)} units` },
      ...ticks.map((t) => svg("line", { class: "gridline", x1: pad.left, x2: width - pad.right,
                                        y1: y(t), y2: y(t) })),
      ...ticks.map((t) => svg("text", { x: pad.left - 6, y: y(t) + 3, "text-anchor": "end" },
        document.createTextNode(num(t, 1)))),
      svg("line", { class: "refline", x1: pad.left, x2: width - pad.right, y1: y(0), y2: y(0) }),
      svg("path", { class: `series ${last >= 0 ? "pos" : "neg"}`, d: path }),
      svg("circle", { class: "dot", cx: x(points.length - 1), cy: y(last), r: 3 }),
    ),
    el("div", { class: "legend" },
      el("span", {}, el("i", { style: `background:${last >= 0 ? "var(--pos)" : "var(--neg)"}` }),
        `Cumulative profit at flat $1 stakes: ${num(last, 2)} units over ${points.length} settled predictions`)));
}

/**
 * Predicted probability against what actually happened, bucketed.
 * The diagonal is perfect calibration; distance from it is the model's honesty gap.
 */
export function calibrationChart(rows, { height = 230 } = {}) {
  const populated = (rows || []).filter((r) => r.n > 0);
  if (!populated.length) {
    return el("div", { class: "state" },
      el("p", { text: "Calibration appears once predictions have settled." }));
  }
  const width = 340;
  const pad = 34;
  const x = scale([0, 1], [pad, width - 12]);
  const y = scale([0, 1], [height - pad, 12]);
  const maxN = Math.max(...populated.map((r) => r.n));

  return el("div", {},
    svg("svg", { class: "chart square", viewBox: `0 0 ${width} ${height}`, role: "img",
                 "aria-label": "Model calibration: predicted probability versus observed frequency" },
      svg("line", { class: "refline", x1: x(0), y1: y(0), x2: x(1), y2: y(1) }),
      svg("line", { class: "axis", x1: pad, y1: y(0), x2: width - 12, y2: y(0) }),
      svg("line", { class: "axis", x1: pad, y1: y(0), x2: pad, y2: 12 }),
      ...[0, 0.5, 1].map((t) => svg("text", { x: x(t), y: height - pad + 14, "text-anchor": "middle" },
        document.createTextNode(pct(t, 0)))),
      ...[0, 0.5, 1].map((t) => svg("text", { x: pad - 6, y: y(t) + 3, "text-anchor": "end" },
        document.createTextNode(pct(t, 0)))),
      ...populated.map((r) =>
        svg("circle", {
          cx: x(r.predicted), cy: y(r.observed),
          // Radius encodes sample size, so a bucket with four predictions looks like one.
          r: 3 + 5 * Math.sqrt(r.n / maxN),
          fill: "var(--accent)", opacity: 0.75,
        }, svg("title", {}, document.createTextNode(
          `${r.bucket}: predicted ${pct(r.predicted)}, observed ${pct(r.observed)} over ${r.n} predictions`)))),
    ),
    el("div", { class: "legend" },
      el("span", {}, el("i", { style: "background:var(--text-dim)" }), "Dashed line = perfect calibration"),
      el("span", {}, el("i", { style: "background:var(--accent)" }), "Dot size = sample count")));
}

/** The model's margin distribution, with key numbers visible as spikes. */
export function distributionChart(dist, { height = 150, marker = null, markerLabel = "" } = {}) {
  if (!dist || !dist.mass || !dist.mass.length) return el("div");
  const width = 640;
  const pad = { top: 10, right: 10, bottom: 20, left: 10 };
  const mass = dist.mass;
  const low = dist.low;
  // Trim the tails to the region that carries the mass, so the shape is readable.
  let start = 0, end = mass.length - 1;
  while (start < end && mass[start] < 0.0015) start += 1;
  while (end > start && mass[end] < 0.0015) end -= 1;
  const slice = mass.slice(start, end + 1);
  const maxMass = Math.max(...slice);
  const x = scale([low + start, low + end], [pad.left, width - pad.right]);
  const y = scale([0, maxMass], [height - pad.bottom, pad.top]);
  const barWidth = Math.max(1.2, (width - pad.left - pad.right) / slice.length - 0.6);

  const labelEvery = Math.ceil(slice.length / 12);
  return el("div", {},
    svg("svg", { class: "chart", viewBox: `0 0 ${width} ${height}`, role: "img",
                 "aria-label": "Projected outcome distribution" },
      ...slice.map((m, i) => {
        const value = low + start + i;
        const isKey = [3, -3, 7, -7, 10, -10, 14, -14].includes(value);
        return svg("rect", {
          class: "barmark", x: x(value) - barWidth / 2, y: y(m),
          width: barWidth, height: Math.max(0, (height - pad.bottom) - y(m)),
          opacity: isKey ? 0.95 : 0.5,
        }, svg("title", {}, document.createTextNode(`${value}: ${pct(m, 2)}`)));
      }),
      marker !== null && marker >= low + start && marker <= low + end
        // Inline style, not a presentation attribute: SVG attributes lose to any CSS rule,
        // so `class="refline"` was repainting this marker in the dim axis colour and the
        // legend swatch promised an amber line the chart never drew.
        ? svg("line", { x1: x(marker), x2: x(marker),
                        y1: pad.top, y2: height - pad.bottom,
                        style: "stroke:var(--warn);stroke-width:1.5;stroke-dasharray:4 3" })
        : null,
      ...slice.filter((_, i) => i % labelEvery === 0).map((_, i) => {
        const value = low + start + i * labelEvery;
        return svg("text", { x: x(value), y: height - 6, "text-anchor": "middle" },
          document.createTextNode(String(value)));
      }),
    ),
    marker !== null
      ? el("div", { class: "legend" },
          el("span", {}, el("i", { style: "background:var(--warn)" }), markerLabel || `Market line ${marker}`),
          el("span", {}, el("i", { style: "background:var(--accent)" }), "Darker bars are NFL key numbers"))
      : el("div", { class: "legend" },
          el("span", {}, el("i", { style: "background:var(--accent)" }), "Darker bars are NFL key numbers")));
}

/** How a contract's price has moved over the snapshots we have stored. */
export function movementChart(history, { height = 120 } = {}) {
  const points = (history || []).filter((h) => h.mid !== null && h.mid !== undefined);
  if (points.length < 2) return null;
  const width = 300;
  const pad = { top: 10, right: 8, bottom: 16, left: 30 };
  const mids = points.map((p) => p.mid);
  const lo = Math.max(0, Math.min(...mids) - 0.03);
  const hi = Math.min(1, Math.max(...mids) + 0.03);
  const x = scale([0, points.length - 1], [pad.left, width - pad.right]);
  const y = scale([lo, hi], [height - pad.bottom, pad.top]);
  const path = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.mid).toFixed(1)}`).join(" ");
  const change = mids[mids.length - 1] - mids[0];
  return el("div", {},
    svg("svg", { class: "chart", viewBox: `0 0 ${width} ${height}`, role: "img",
                 "aria-label": "Price history" },
      ...[lo, hi].map((t) => svg("text", { x: pad.left - 5, y: y(t) + 3, "text-anchor": "end" },
        document.createTextNode(pct(t, 0)))),
      svg("path", { class: `series ${change >= 0 ? "pos" : "neg"}`, d: path })));
}

/** A horizontal split showing two win probabilities against each other. */
export function splitBar(homeProb, awayProb, homeLabel, awayLabel) {
  const total = homeProb + awayProb || 1;
  return el("div", {},
    el("div", { class: "bar" },
      el("span", { class: "away", style: `width:${(awayProb / total) * 100}%` }),
      el("span", { class: "home", style: `width:${(homeProb / total) * 100}%` })),
    el("div", { class: "legend" },
      el("span", {}, el("i", { style: "background:var(--border-strong)" }), `${awayLabel} ${pct(awayProb)}`),
      el("span", {}, el("i", { style: "background:var(--accent)" }), `${homeLabel} ${pct(homeProb)}`)));
}

/** Ranked horizontal bars — used for rating comparisons where order is the message. */
export function rankBars(rows, { valueKey = "value", labelKey = "label", height = 14,
                                 digits = 3, format = null } = {}) {
  if (!rows.length) return el("div");
  const values = rows.map((r) => r[valueKey]);
  const max = Math.max(...values.map(Math.abs)) || 1;
  // A centred axis only earns its space when values actually go both ways. When they are
  // all one sign, centring throws away half the width and makes every bar look shorter
  // than it is.
  const diverging = Math.min(...values) < 0 && Math.max(...values) > 0;
  const span = diverging ? 50 : 100;

  return el("div", { style: "display:flex;flex-direction:column;gap:4px" },
    ...rows.map((r) => {
      const v = r[valueKey];
      const width = (Math.abs(v) / max) * span;
      const left = diverging ? (v >= 0 ? 50 : 50 - width) : 0;
      return el("div", { style: "display:grid;grid-template-columns:46px 1fr 62px;gap:8px;align-items:center" },
        el("span", { class: "mono-sm", text: r[labelKey] }),
        el("div", { style: `height:${height}px;position:relative;background:var(--bg-raised);border-radius:3px` },
          el("div", {
            style: `position:absolute;top:0;bottom:0;left:${left}%;`
                 + `width:${width}%;border-radius:2px;`
                 + `background:${v >= 0 ? "var(--pos)" : "var(--neg)"};opacity:.8`,
          }),
          diverging
            ? el("div", { style: "position:absolute;top:0;bottom:0;left:50%;width:1px;background:var(--border-strong)" })
            : null),
        el("span", { class: "num dim", style: "font-size:11px;text-align:right",
                     text: format ? format(r) : num(v, digits) }));
    }));
}
