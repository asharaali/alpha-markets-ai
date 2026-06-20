/* Alpha Markets AI — frontend logic (no build step, plain ES modules-free JS) */

const API = ""; // same origin
let comboLegs = [];
let pollTimer = null;

/* ---------- tabs ---------- */
document.querySelectorAll(".tab").forEach((t) => {
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    t.classList.add("active");
    document.getElementById(t.dataset.tab).classList.add("active");
    if (t.dataset.tab === "kalshi") loadKalshi();
    if (t.dataset.tab === "record") loadRecord();
  });
});

/* ---------- helpers ---------- */
const pct = (x) => (x * 100).toFixed(0) + "%";
const money = (x) => "$" + Number(x).toFixed(2);
function evClass(ev) { return ev > 0 ? "pos" : "neg"; }

/* ---------- dashboard ---------- */
async function loadMatches() {
  try {
    const res = await fetch(`${API}/api/matches`);
    const data = await res.json();
    setLive(true, data);
    updateDataBanner(data);
    renderMatches(data.matches);
    // Adaptive cadence: fast while a game is in play, hourly when idle.
    const next = (data.poll_seconds || 3600) * 1000;
    clearTimeout(pollTimer);
    pollTimer = setTimeout(loadMatches, next);
  } catch (e) {
    setLive(false);
    document.getElementById("matches").innerHTML =
      `<div class="empty">Backend not reachable. Start it with <code>./run.sh</code>.</div>`;
    clearTimeout(pollTimer);
    pollTimer = setTimeout(loadMatches, 30000);
  }
}

function updateDataBanner(data) {
  const el = document.getElementById("dataBanner");
  if (!el) return;
  if (data.data_live) { el.className = "data-banner"; el.innerHTML = ""; return; }
  el.className = "data-banner show";
  el.innerHTML = `⚠️ <b>Not live data.</b> ${data.data_reason || "Live odds unavailable"} — the games below are <b>sample data, not real fixtures. Do not bet off these.</b>`;
}

function setLive(on, data) {
  document.getElementById("liveDot").classList.toggle("on", on);
  const label = document.getElementById("liveLabel");
  if (!on) { label.textContent = "offline"; return; }
  if (data.any_live) {
    label.textContent = `LIVE GAME · updating every ${data.poll_seconds}s`;
  } else {
    label.textContent = "no live game · hourly refresh";
  }
  const badge = document.getElementById("modeBadge");
  if (data.demo_mode === undefined) return;
  badge.textContent = data.demo_mode ? "DEMO DATA" : "LIVE ODDS";
  badge.className = "badge " + (data.demo_mode ? "demo" : "live");
}

let allMatches = [];
let boardFilter = "today";

function renderMatches(matches) {
  allMatches = matches || [];
  applyFilter();
}

function applyFilter() {
  const wrap = document.getElementById("matches");
  const now = new Date();
  const isSameLocalDay = (iso) => {
    const d = new Date(iso);
    return d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  };
  let list = allMatches;
  if (boardFilter === "today") {
    list = allMatches.filter((m) => m.status === "live" || isSameLocalDay(m.commence_time));
  } else if (boardFilter === "upcoming") {
    const wk = new Date(now.getTime() + 7 * 864e5);
    list = allMatches.filter((m) => m.status === "live" || new Date(m.commence_time) <= wk);
  }
  if (!list.length) {
    wrap.innerHTML = `<div class="empty">No games in this view. Try <b>Next 7 days</b> or <b>All</b>.</div>`;
    return;
  }
  wrap.innerHTML = list.map(matchCard).join("");
  wrap.querySelectorAll(".combo-btn").forEach((b) => {
    b.addEventListener("click", () => addLegFromBtn(b));
  });
}

document.querySelectorAll(".filt").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll(".filt").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    boardFilter = b.dataset.filt;
    applyFilter();
  });
});

function matchCard(m) {
  const p = m.model.probs;
  const live = m.status === "live";
  const head = live
    ? `<div class="live-tag">LIVE ${m.live_minute || 0}'</div>
       <div class="score">${m.live_score.home}–${m.live_score.away}</div>`
    : `<div class="meta">${new Date(m.commence_time).toLocaleString([], {month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"})}</div>`;

  // Market-implied probabilities (from the book's odds) so you can compare to our model.
  let marketLine = "";
  const mr = (m.suggestions || []).filter((s) => s.market === "Match Result");
  if (mr.length === 3) {
    const g = (name) => mr.find((s) => s.selection === name);
    const h = g(m.home), dr = g("Draw"), a = g(m.away);
    if (h && dr && a) {
      marketLine = `<div class="marketline">Market (book/Kalshi) · ${m.home} ${pct(h.market_prob)} · Draw ${pct(dr.market_prob)} · ${m.away} ${pct(a.market_prob)}</div>`;
    }
  }

  // Live win-probability shift vs pre-game (so you can see momentum swing).
  let liveShift = "";
  if (live && m.model.pregame_probs) {
    const pre = m.model.pregame_probs;
    const dH = (p.home - pre.home) * 100;
    const arrow = (d) => d > 1 ? `<span class="up">▲${d.toFixed(0)}</span>` : d < -1 ? `<span class="down">▼${Math.abs(d).toFixed(0)}</span>` : "";
    liveShift = `<div class="live-shift">live vs pre-game: ${m.home} ${arrow(dH)} · exp final ${m.model.expected_final_goals.a}–${m.model.expected_final_goals.b}</div>`;
  }

  const rows = m.suggestions.map((s) => {
    const star = s.value_bet ? `<span class="star" title="Model sees value">★</span>` : "";
    // Every pick can be added to a parlay now — not just flagged value bets.
    const comboBtn = `<button class="combo-btn" data-label="${m.home} v ${m.away}: ${s.selection}" data-prob="${s.model_prob}" data-odds="${s.market_odds_decimal}" data-home="${m.home}" data-away="${m.away}" data-market="${s.market}" data-sel="${s.selection}">＋ combo</button>`;
    const detail = s.value_bet
      ? `Edge ${(s.edge*100).toFixed(1)}% · stake ${money(s.stake.recommended_dollars)} (${s.stake.recommended_pct}% bank)`
      : `model ${(s.model_prob*100).toFixed(0)}%`;
    return `<div class="sugg-row">
      <span class="name"><span class="tier-dot ${s.tier}"></span>${s.selection} ${star}</span>
      <span class="odds">${s.market_odds_decimal} <small>(${s.market_odds_american>0?"+":""}${s.market_odds_american})</small></span>
      <span class="ev ${evClass(s.ev_per_dollar)}">${s.ev_per_dollar>0?"+":""}${(s.ev_per_dollar*100).toFixed(1)}%</span>
    </div>
    <div class="sub">${detail} ${comboBtn}</div>`;
  }).join("");

  return `<div class="card">
    <div class="card-head">
      <div><div class="teams">${m.home} <span class="muted">vs</span> ${m.away}</div>
      <div class="meta">${m.league}${m.best_value ? ` · ★ best value: ${m.best_value.selection}` : ""}</div></div>
      <div>${head}</div>
    </div>
    <div class="probbar">
      <span class="h" style="width:${p.home*100}%"></span>
      <span class="d" style="width:${p.draw*100}%"></span>
      <span class="a" style="width:${p.away*100}%"></span>
    </div>
    <div class="problabels"><span><b>Our model</b> · ${m.home} ${pct(p.home)}</span><span>Draw ${pct(p.draw)}</span><span>${m.away} ${pct(p.away)}</span></div>
    ${marketLine}
    ${liveShift}
    <div class="sugg">${rows}</div>
    <button class="markets-btn" data-home="${m.home}" data-away="${m.away}">＋ all markets — goals, scores, player props ▾</button>
    <div class="markets-box" id="mk-${m.id}"></div>
  </div>`;
}

/* ---------- expanded markets (goals O/U, team totals, correct score, player props) ---------- */
async function toggleMarkets(btn) {
  const box = btn.nextElementSibling;
  if (box.classList.contains("open")) {
    box.classList.remove("open"); box.innerHTML = ""; btn.innerHTML = btn.innerHTML.replace("▴", "▾");
    return;
  }
  box.innerHTML = `<div class="empty" style="padding:14px">Loading markets…</div>`;
  box.classList.add("open");
  btn.innerHTML = btn.innerHTML.replace("▾", "▴");
  const home = btn.dataset.home, away = btn.dataset.away;
  const d = await (await fetch(`${API}/api/markets?team_a=${encodeURIComponent(home)}&team_b=${encodeURIComponent(away)}`)).json();
  // Markets Kalshi lets you put in a parlay: who wins, win margin/spread, total goals, BTTS, goalscorers.
  const kalshiParlay = (cat) => /match result|winning margin|spread|total goals|both teams|goalscorer/i.test(cat);
  box.innerHTML = Object.entries(d.markets).map(([cat, sels]) => `
    <div class="mk-cat">${cat} ${kalshiParlay(cat)
      ? '<span class="kbadge ok">Kalshi parlay ✓</span>'
      : '<span class="kbadge no">other books only</span>'}</div>
    ${sels.map((s) => `
      <div class="mk-row">
        <span>${s.label}</span>
        <span class="mk-prob">${(s.prob*100).toFixed(0)}%</span>
        <span class="mk-odds">${s.fair_odds}</span>
        <button class="combo-btn" data-label="${home} v ${away}: ${s.label}" data-prob="${s.prob}" data-odds="${s.fair_odds}" data-home="${home}" data-away="${away}" data-market="${cat}" data-sel="${s.label}">＋</button>
      </div>`).join("")}
  `).join("");
  box.querySelectorAll(".combo-btn").forEach((b) => {
    b.addEventListener("click", () => addLegFromBtn(b));
  });
}

document.getElementById("matches").addEventListener("click", (e) => {
  const btn = e.target.closest(".markets-btn");
  if (btn) toggleMarkets(btn);
});

/* ---------- combo builder ---------- */
function addLegFromBtn(b) {
  const d = b.dataset;
  addComboLeg(d.label, parseFloat(d.prob), parseFloat(d.odds), d.home, d.away, d.market, d.sel);
}
function addComboLeg(label, prob, odds, home, away, market, selection) {
  comboLegs.push({ label, model_prob: prob, market_odds_decimal: odds,
    home: home || null, away: away || null, market: market || null, selection: selection || null });
  renderComboLegs();
  // jump to combo tab
  document.querySelector('.tab[data-tab="combo"]').click();
}
function renderComboLegs() {
  const wrap = document.getElementById("comboLegs");
  wrap.innerHTML = comboLegs.map((l, i) =>
    `<div class="leg">${l.label} · p=${l.model_prob} · ${l.market_odds_decimal} <button data-i="${i}">×</button></div>`
  ).join("");
  wrap.querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => { comboLegs.splice(b.dataset.i, 1); renderComboLegs(); }));
}
document.getElementById("addLeg").addEventListener("click", () => {
  const label = document.getElementById("legLabel").value || "Leg";
  const prob = parseFloat(document.getElementById("legProb").value);
  const odds = parseFloat(document.getElementById("legOdds").value);
  if (!prob || !odds) return alert("Enter a model prob (0-1) and decimal odds.");
  addComboLeg(label, prob, odds);
  document.getElementById("legLabel").value = "";
  document.getElementById("legProb").value = "";
  document.getElementById("legOdds").value = "";
});
let lastCombo = null;
document.getElementById("evalCombo").addEventListener("click", async () => {
  if (!comboLegs.length) return alert("Add at least one leg.");
  const res = await fetch(`${API}/api/combo`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ legs: comboLegs }),
  });
  const d = await res.json();
  lastCombo = d;
  const card = document.getElementById("comboResult");
  card.className = "result-card show " + (d.value_bet ? "go" : "warn");
  const adj = d.reality_factor
    ? `<div>Experience-adj prob<b>${pct(d.experience_adjusted_prob)}</b></div>` : "";
  card.innerHTML = `
    <div class="action-line" style="color:${d.value_bet ? "var(--green)" : "var(--red)"}">
      ${d.value_bet ? "✓ +EV COMBO" : "✗ -EV — skip or trim legs"}</div>
    <div>Tier: <b style="display:inline">${d.tier.toUpperCase()}</b></div>
    <div class="kv">
      <div>Combined prob<b>${pct(d.combined_model_prob)}</b></div>
      ${adj}
      <div>Payout<b>${d.payout_multiple}×</b></div>
      <div>American<b>${d.combined_odds_american>0?"+":""}${d.combined_odds_american}</b></div>
      <div>EV / $1<b style="color:${d.value_bet?"var(--green)":"var(--red)"}">${d.ev_per_dollar>0?"+":""}${(d.ev_per_dollar*100).toFixed(1)}%</b></div>
      <div>Suggested stake<b>${money(d.stake.recommended_dollars)}</b></div>
    </div>
    <p class="sub">${d.note}</p>
    <button id="logBet" class="btn primary" style="margin-top:12px">＋ Log this bet to My Record</button>`;
  document.getElementById("logBet").addEventListener("click", logCurrentCombo);
});

async function logCurrentCombo() {
  if (!lastCombo) return;
  const stake = parseFloat(document.getElementById("comboStake").value) || 0;
  const book = document.getElementById("comboBook").value || "";
  await fetch(`${API}/api/combo/log`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ combo: lastCombo, stake, book }),
  });
  const btn = document.getElementById("logBet");
  btn.textContent = "✓ Logged — settle it in My Record";
  btn.disabled = true;
  loadRecord();
}

/* ---------- Kalshi ---------- */
async function loadKalshi() {
  const wrap = document.getElementById("kalshiGames");
  wrap.innerHTML = `<div class="empty">Loading Kalshi markets…</div>`;
  try {
    const d = await (await fetch(`${API}/api/kalshi`)).json();
    if (!d.games.length) { wrap.innerHTML = `<div class="empty">No Kalshi World Cup markets open.</div>`; return; }
    const note = d.tradeable === 0
      ? `<div class="empty" style="grid-column:1/-1">${d.count} World Cup events found, but Kalshi has no live prices on them yet (untraded order books). They'll populate as games get liquidity — the model overlay is ready.</div>` : "";
    wrap.innerHTML = note + d.games.map(kalshiCard).join("");
  } catch (e) {
    wrap.innerHTML = `<div class="empty">Couldn't reach Kalshi.</div>`;
  }
}

function kalshiCard(g) {
  if (!g.tradeable) {
    return `<div class="card dim">
      <div class="teams">${g.home} <span class="muted">vs</span> ${g.away}</div>
      <div class="meta">${g.note || "untraded"}</div></div>`;
  }
  const rows = g.sides.map((s) => {
    const star = s.value_bet ? `<span class="star">★</span>` : "";
    return `<div class="sugg-row">
      <span class="name"><span class="tier-dot ${s.tier}"></span>${s.selection} ${star}</span>
      <span class="odds">${s.kalshi_price_cents}¢ <small>model ${(s.model_prob*100).toFixed(0)}%</small></span>
      <span class="ev ${s.ev_per_dollar>0?'pos':'neg'}">${s.ev_per_dollar>0?"+":""}${(s.ev_per_dollar*100).toFixed(1)}%</span>
    </div>`;
  }).join("");
  return `<div class="card">
    <div class="card-head"><div class="teams">${g.home} <span class="muted">vs</span> ${g.away}</div>
    <div class="meta">${g.value_count} value</div></div>
    <div class="sugg">${rows}</div></div>`;
}

/* ---------- Record / learning ---------- */
let recordPoll = null;
async function loadRecord() {
  const [d, live] = await Promise.all([
    (await fetch(`${API}/api/bets`)).json(),
    (await fetch(`${API}/api/bets/live`)).json(),
  ]);
  const liveMap = {};
  (live.bets || []).forEach((b) => (liveMap[b.id] = b));
  const rf = d.reality_factor;
  document.getElementById("recordStats").innerHTML = `
    <div class="kv">
      <div>Record<b>${d.hits}–${d.misses}</b></div>
      <div>Hit rate<b>${d.hit_rate!=null?pct(d.hit_rate):"—"}</b></div>
      <div>Staked<b>${money(d.staked)}</b></div>
      <div>Net<b style="color:${d.net>=0?'var(--green)':'var(--red)'}">${d.net>=0?"+":""}${money(d.net)}</b></div>
      <div>ROI<b style="color:${(d.roi||0)>=0?'var(--green)':'var(--red)'}">${d.roi!=null?(d.roi*100).toFixed(0)+"%":"—"}</b></div>
      <div>Reality factor<b>${rf!=null?rf:"—"}</b></div>
    </div>
    <p class="sub" style="margin-top:10px">${d.learning}</p>`;

  document.getElementById("pendingBets").innerHTML = d.pending_bets.length
    ? d.pending_bets.map((b) => pendingRow(b, liveMap[b.id])).join("")
    : `<div class="empty">No pending bets. Log a combo to start tracking.</div>`;
  d.pending_bets.forEach((b) => {
    document.getElementById(`hit-${b.id}`).onclick = () => settle(b.id, true);
    document.getElementById(`miss-${b.id}`).onclick = () => settle(b.id, false);
  });

  document.getElementById("settledBets").innerHTML = d.recent_settled.length
    ? d.recent_settled.map(settledRow).join("")
    : `<div class="empty">Nothing settled yet.</div>`;

  // Re-poll the live cash-out monitor while a game is in play and this tab is open.
  clearTimeout(recordPoll);
  if (live.any_live && document.getElementById("record").classList.contains("active")) {
    recordPoll = setTimeout(loadRecord, 30000);
  }
}

const legLabels = (b) => (b.legs || []).map((l) => (typeof l === "string" ? l : l.label)).join(" + ");

function cashoutTag(ls) {
  if (!ls) return "";
  if (ls.cash_out) {
    return `<div class="cashout-light on" title="${ls.action}">🔴 CASH OUT NOW</div>
            <div class="live-health">live ${(ls.live_prob*100).toFixed(0)}% vs entry ${(ls.entry_prob*100).toFixed(0)}% — it's slipping, bail if Kalshi offers a fair price</div>`;
  }
  if (ls.any_live) {
    return `<div class="cashout-light off">🟢 HOLD — on track</div>
            <div class="live-health on-track">live ${(ls.live_prob*100).toFixed(0)}% vs entry ${(ls.entry_prob*100).toFixed(0)}% — don't cash out, let it ride</div>`;
  }
  return `<div class="cashout-light idle">⚪ cash-out alert: armed (game not live yet)</div>`;
}

function pendingRow(b, ls) {
  return `<div class="bet ${ls && ls.cash_out ? "alert" : ""}">
    <div class="bet-info"><b>${legLabels(b)}</b>
      <div class="meta">${b.leg_count} legs · model ${(b.model_prob*100).toFixed(0)}% · ${b.payout_multiple}× · $${b.stake} on ${b.book||"—"}</div>
      ${cashoutTag(ls)}</div>
    <div class="bet-actions">
      <button id="hit-${b.id}" class="btn hit">Hit ✓</button>
      <button id="miss-${b.id}" class="btn miss">Miss ✗</button>
    </div></div>`;
}

function settledRow(b) {
  const win = b.status === "hit";
  return `<div class="bet">
    <div class="bet-info"><b>${legLabels(b)}</b>
      <div class="meta">model ${(b.model_prob*100).toFixed(0)}% · ${b.payout_multiple}× · $${b.stake}</div></div>
    <span class="result-badge ${win?'won':'lost'}">${win?"HIT":"MISS"}</span></div>`;
}

async function settle(id, hit) {
  await fetch(`${API}/api/combo/settle`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ bet_id: id, hit }),
  });
  loadRecord();
}

/* ---------- model badge ---------- */
async function loadModelInfo() {
  try {
    const d = await (await fetch(`${API}/api/model-info`)).json();
    const b = document.getElementById("modelBadge");
    if (d.trained) {
      b.textContent = `model ✓ ${(d.metrics.accuracy*100).toFixed(0)}% · ${d.n_teams} teams`;
      b.title = `Trained on ${d.n_matches_trained} matches. Out-of-sample log loss ${d.metrics.log_loss.toFixed(3)} (baseline ${d.baseline_log_loss}).`;
      b.classList.add("live");
    } else {
      b.textContent = "model: untrained";
    }
  } catch (e) {}
}

/* ---------- cash-out ---------- */
document.getElementById("evalCashout").addEventListener("click", async () => {
  const body = {
    entry_price: parseFloat(document.getElementById("coEntry").value),
    current_market_price: parseFloat(document.getElementById("coCurrent").value),
    model_prob: parseFloat(document.getElementById("coModel").value),
    stake: parseFloat(document.getElementById("coStake").value),
  };
  const res = await fetch(`${API}/api/cashout`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const d = await res.json();
  const card = document.getElementById("cashoutResult");
  const isExit = /CASH OUT|EXIT/.test(d.action);
  card.className = "result-card show " + (isExit ? "warn" : "go");
  card.innerHTML = `
    <div class="action-line" style="color:${isExit ? "var(--amber)" : "var(--green)"}">${d.action}</div>
    <p>${d.reason}</p>
    ${d.tables_turned ? `<p class="sub" style="color:var(--red)">⚠ Tables turned — model fair value has dropped sharply below your entry.</p>` : ""}
    <div class="kv">
      <div>Entry<b>${(d.entry_price*100).toFixed(0)}¢</b></div>
      <div>Sell now<b>${(d.current_market_price*100).toFixed(0)}¢</b></div>
      <div>Model fair value<b>${(d.model_fair_value*100).toFixed(0)}¢</b></div>
      <div>Unrealized P/L<b style="color:${d.unrealized_pnl_pct>=0?"var(--green)":"var(--red)"}">${d.unrealized_pnl_pct>0?"+":""}${d.unrealized_pnl_pct}%</b></div>
      <div>Cash-out value<b>${money(d.cashout_value)}</b></div>
    </div>`;
});

/* ---------- auto-build parlay ---------- */
const localDate = (iso) => new Date(iso).toLocaleDateString("en-CA"); // YYYY-MM-DD local
document.querySelectorAll(".btn.ab").forEach((b) => {
  b.addEventListener("click", async () => {
    const style = b.dataset.style;
    const dateVal = document.getElementById("abDate").value;
    const data = await (await fetch(`${API}/api/matches`)).json();
    const games = data.matches
      .filter((m) => m.status !== "completed" && (!dateVal || localDate(m.commence_time) === dateVal))
      .slice(0, 8).map((m) => ({ home: m.home, away: m.away }));
    if (!games.length) return alert("No games found for that date. Try another day or the All filter on the Live Board.");
    const r = await (await fetch(`${API}/api/parlay/auto`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ games, style }),
    })).json();
    if (r.error) return alert(r.error);
    comboLegs = r.legs.map((l) => ({
      label: l.label, model_prob: l.model_prob, market_odds_decimal: l.market_odds_decimal,
      home: l.home, away: l.away, market: l.market, selection: l.selection,
    }));
    renderComboLegs();
    document.getElementById("evalCombo").click();
  });
});
// default the date picker to today
(() => { const d = document.getElementById("abDate"); if (d) d.value = new Date().toLocaleDateString("en-CA"); })();

/* ---------- auth / login gate ---------- */
let currentUser = null, currentTopic = null;

async function checkAuth() {
  try {
    const d = await (await fetch(`${API}/api/me`)).json();
    if (d.user) { currentUser = d.user; currentTopic = d.ntfy_topic; enterApp(); }
    else document.getElementById("loginGate").classList.add("show");
  } catch (e) {
    document.getElementById("loginGate").classList.add("show");
  }
}

function enterApp() {
  document.getElementById("loginGate").classList.remove("show");
  document.getElementById("userTag").innerHTML = `👤 ${currentUser} · <a id="logoutBtn">log out</a>`;
  document.getElementById("logoutBtn").onclick = async () => {
    await fetch(`${API}/api/logout`, { method: "POST" });
    location.reload();
  };
  loadModelInfo();
  loadMatches();  // self-schedules its next poll based on whether a game is live
}

async function doAuth(path) {
  const username = document.getElementById("liUser").value.trim();
  const password = document.getElementById("liPass").value;
  const err = document.getElementById("liError");
  err.textContent = "";
  if (!username || !password) { err.textContent = "enter a username and password"; return; }
  const res = await fetch(`${API}${path}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  const d = await res.json();
  if (!res.ok) { err.textContent = d.error || "failed"; return; }
  currentUser = d.user; currentTopic = d.ntfy_topic;
  enterApp();
  if (path.includes("signup")) {
    setTimeout(() => alert(`Welcome, ${currentUser}! 🎉\n\nYour bets are private to your account.\n\n🔔 For phone alerts: open the ntfy app and subscribe to YOUR topic:\n\n${currentTopic}`), 350);
  }
}
document.getElementById("liLogin").addEventListener("click", () => doAuth("/api/login"));
document.getElementById("liSignup").addEventListener("click", () => doAuth("/api/signup"));
document.getElementById("liPass").addEventListener("keydown", (e) => { if (e.key === "Enter") doAuth("/api/login"); });

/* ---------- boot ---------- */
checkAuth();
