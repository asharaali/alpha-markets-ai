/* Shared plumbing: the API client, formatting, and the UI primitives every view uses. */

/* ---------------------------------------------------------------- API client */

class ApiError extends Error {
  constructor(message, status, code) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      credentials: "same-origin",
      headers: options.body ? { "Content-Type": "application/json" } : {},
      ...options,
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
  } catch (err) {
    // A network failure is not the same as a server error, and the UI says so.
    throw new ApiError("Could not reach the server. Check that it is running.", 0, "network");
  }
  let payload = null;
  try { payload = await response.json(); } catch { payload = null; }
  if (!response.ok) {
    const message = payload?.message
      || payload?.detail?.[0]?.msg
      || `Request failed (${response.status}).`;
    throw new ApiError(message, response.status, payload?.error);
  }
  return payload;
}

export { ApiError };

/* ---------------------------------------------------------------- formatting */

export const pct = (v, digits = 1) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : `${(v * 100).toFixed(digits)}%`;

export const signedPct = (v, digits = 1) => {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const s = (v * 100).toFixed(digits);
  return `${v > 0 ? "+" : ""}${s}%`;
};

export const num = (v, digits = 2) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : Number(v).toFixed(digits);

export const signed = (v, digits = 1) => {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${v > 0 ? "+" : ""}${Number(v).toFixed(digits)}`;
};

export const money = (v) =>
  v === null || v === undefined || Number.isNaN(v)
    ? "—"
    : `${v < 0 ? "-" : ""}$${Math.abs(Number(v)).toLocaleString(undefined, {
        minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export const compactMoney = (v) =>
  v === null || v === undefined ? "—"
    : Math.abs(v) >= 1000 ? `$${(v / 1000).toFixed(1)}k` : `$${Math.round(v)}`;

export const cents = (v) =>
  v === null || v === undefined ? "—" : `${Math.round(v * 100)}¢`;

export function kickoffLabel(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    weekday: "short", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit",
  });
}

export function relativeTime(seconds) {
  if (!seconds) return "—";
  const delta = Date.now() / 1000 - seconds;
  if (delta < 60) return "just now";
  if (delta < 3600) return `${Math.round(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.round(delta / 3600)}h ago`;
  return `${Math.round(delta / 86400)}d ago`;
}

export const evClass = (v) => (v === null || v === undefined ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "");

/* ------------------------------------------------------------------ elements */

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "html") node.innerHTML = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "dataset") {
      Object.assign(node.dataset, value);
    } else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export const frag = (...children) => {
  const f = document.createDocumentFragment();
  for (const c of children.flat()) if (c) f.append(c);
  return f;
};

/* --------------------------------------------------------------- primitives */

export function panel(title, { sub, actions, flush = false } = {}, ...body) {
  const head = el("div", { class: "panel-head" },
    el("h2", { text: title }),
    sub ? el("span", { class: "sub", text: sub }) : null,
    actions ? el("div", { class: "spacer" }) : null,
    actions || null,
  );
  return el("section", { class: "panel" }, head,
    el("div", { class: `panel-body${flush ? " flush" : ""}` }, ...body));
}

export function stat(label, value, note, cls = "") {
  return el("div", { class: "stat" },
    el("div", { class: "stat-label", text: label }),
    el("div", { class: `stat-value ${cls}`, text: value }),
    note ? el("div", { class: "stat-note", text: note }) : null,
  );
}

export const statRow = (...tiles) => el("div", { class: "stats" }, ...tiles.flat());

export function badge(text, kind = "") {
  return el("span", { class: `badge ${kind}`, text });
}

export function loading(rows = 5) {
  return el("div", {},
    ...Array.from({ length: rows }, () =>
      el("div", { class: "skeleton-row" },
        el("div", { class: "skeleton" }),
        el("div", { class: "skeleton" }),
        el("div", { class: "skeleton" }))),
  );
}

export function emptyState(title, message, action) {
  return el("div", { class: "state" },
    el("h3", { text: title }),
    el("p", { text: message }),
    action || null);
}

export function errorState(err, retry) {
  const message = err instanceof ApiError && err.status === 0
    ? "The server is not responding. If you are running locally, check that it is still up."
    : err?.message || "Something went wrong.";
  return el("div", { class: "state error" },
    el("h3", { text: "Could not load this view" }),
    el("p", { text: message }),
    retry ? el("button", { class: "btn", onClick: retry, text: "Try again" }) : null);
}

export function notice(text, kind = "") {
  return el("div", { class: `notice ${kind}`, html: text });
}

export function table(columns, rows, { onRowClick, empty } = {}) {
  if (!rows.length) {
    return emptyState("Nothing here", empty || "No rows to show.");
  }
  const thead = el("thead", {}, el("tr", {},
    ...columns.map((c) => el("th", { class: c.num ? "num" : "", text: c.label }))));
  const tbody = el("tbody", {},
    ...rows.map((row) => {
      const tr = el("tr", { class: onRowClick ? "clickable" : "" },
        ...columns.map((c) => {
          const value = c.render ? c.render(row) : row[c.key];
          const cls = [c.num ? "num" : "", c.cls ? c.cls(row) : ""].filter(Boolean).join(" ");
          return value instanceof Node
            ? el("td", { class: cls }, value)
            : el("td", { class: cls, text: value === null || value === undefined ? "—" : String(value) });
        }));
      if (onRowClick) tr.addEventListener("click", () => onRowClick(row));
      return tr;
    }));
  return el("div", { class: "table-scroll" }, el("table", { class: "data" }, thead, tbody));
}

export function confidenceBadge(level) {
  return badge(level || "—", level || "");
}

export function probRow(modelProb, marketProb, edge) {
  return el("div", { class: "probrow" },
    el("div", { class: "probcell" },
      el("div", { class: "k", text: "Model" }),
      el("div", { class: "v", text: pct(modelProb) })),
    el("div", { class: "probcell" },
      el("div", { class: "k", text: "Market" }),
      el("div", { class: "v", text: pct(marketProb) })),
    el("div", { class: "probcell" },
      el("div", { class: "k", text: "Edge" }),
      el("div", { class: `v ${evClass(edge)}`, text: signedPct(edge) })),
  );
}

export function disclosure(summary, ...body) {
  return el("details", { class: "disclosure" },
    el("summary", { text: summary }),
    el("div", { class: "disclosure-body" }, ...body));
}

/** Reads a value from an object by dotted path, tolerating missing links. */
export const get = (obj, path, fallback = null) =>
  path.split(".").reduce((acc, key) => (acc && acc[key] !== undefined ? acc[key] : undefined), obj)
  ?? fallback;
