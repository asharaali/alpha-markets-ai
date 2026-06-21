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
    if (t.dataset.tab === "autobet") loadAutobet();
    if (t.dataset.tab === "combo") loadComboGames();
    if (t.dataset.tab === "weather") loadWeather();
  });
});

/* ---------- weather markets ---------- */
async function loadWeather() {
  const status = document.getElementById("weatherStatus");
  status.textContent = "Loading forecasts + order books…";
  let d;
  try {
    d = await (await fetch(`${API}/api/weather`)).json();
  } catch (e) { status.textContent = "Couldn't load weather markets."; return; }
  status.innerHTML = `Priced <b>${d.count}</b> contracts across ${d.cities.length} cities · `
    + `<b>${d.value_count}</b> value bet${d.value_count === 1 ? "" : "s"} found (min edge ${Math.round(d.min_edge * 100)} pts).`;

  const value = document.getElementById("weatherValue");
  value.innerHTML = d.value_bets.length
    ? d.value_bets.map(weatherValueRow).join("")
    : `<div class="empty">No +EV weather bets right now — the honest move is no bet. Check back as forecasts update.</div>`;

  const all = document.getElementById("weatherAll");
  all.innerHTML = d.all_markets.map(weatherMarketRow).join("");
  loadWeatherCalibration();
}
async function loadWeatherCalibration() {
  const el = document.getElementById("weatherCalib");
  if (!el) return;
  try {
    const c = await (await fetch(`${API}/api/weather/calibration`)).json();
    const rows = (c.by_lead || []).map((l) =>
      `<div class="meta">${l.lead_days}d out: model assumes ±${l.assumed_sigma_f}°F, actual avg miss ${l.mae_f}°F (n=${l.n})</div>`).join("");
    el.innerHTML = `<div class="sub"><b>Model calibration:</b> ${c.status}</div>${rows}`;
  } catch (e) { el.textContent = ""; }
}
function weatherValueRow(v) {
  const conf = { high: "var(--green)", medium: "#d8a657", low: "var(--muted)" }[v.confidence];
  return `<div class="bet">
    <div class="bet-info"><b>BUY ${v.side} · ${v.city} ${v.label}</b>
      <div class="meta">${v.date} · ${v.lead_days}d out · NWS forecast ${v.forecast_high_f}°F · depth $${v.depth}</div>
      <div class="sub">model ${pct(v.model_prob_shrunk)} vs market ${Math.round(v.price * 100)}¢ → edge <b style="color:var(--green)">+${Math.round(v.edge * 100)} pts</b> · EV +${Math.round(v.ev_per_dollar * 100)}%/$ · suggested $${v.kelly_stake}</div></div>
    <span class="result-badge won" style="background:${conf}22;color:${conf}">${v.confidence} conf</span></div>`;
}
function weatherMarketRow(m) {
  const bid = m.yes_bid != null ? Math.round(m.yes_bid * 100) + "¢" : "—";
  const ask = m.yes_ask != null ? Math.round(m.yes_ask * 100) + "¢" : "—";
  return `<div class="bet">
    <div class="bet-info"><b>${m.city} ${m.label}</b>
      <div class="meta">${m.date} · ${m.lead_days}d · NWS ${m.forecast_high_f}°F</div></div>
    <div class="sub" style="text-align:right">model <b>${pct(m.model_prob)}</b><br>mkt ${bid}/${ask}</div></div>`;
}

/* ---------- auto-bet ---------- */
async function loadAutobet() {
  const d = await (await fetch(`${API}/api/autobet`)).json();
  const c = d.config;
  if (document.getElementById("abTopic")) document.getElementById("abTopic").textContent = currentTopic || "—";
  document.getElementById("abEnabled").value = String(c.enabled);
  document.getElementById("abMode").value = c.mode;
  document.getElementById("abMaxStake").value = c.max_stake;
  document.getElementById("abDailyCap").value = c.daily_cap;
  document.getElementById("abMinEdge").value = Math.round(c.min_edge * 100);
  document.getElementById("abMaxBets").value = c.max_bets_day;
  const liveOpt = document.querySelector('#abMode option[value="live"]');
  liveOpt.disabled = !d.live_available;
  liveOpt.textContent = d.live_available ? "💸 Live (real money)" : "💸 Live (needs Kalshi key)";
  document.getElementById("abStatus").innerHTML = `
    <div class="kv">
      <div>Status<b style="color:${c.enabled ? 'var(--green)' : 'var(--muted)'}">${c.enabled ? "ARMED · " + c.mode.toUpperCase() : "OFF"}</b></div>
      <div>Today<b>${d.today_count} bets · $${d.today_spend}</b></div>
      <div>Remaining today<b>$${d.remaining_today}</b></div>
      <div>Hard caps<b>$${d.hard_max_stake}/bet · $${d.hard_daily_cap}/day</b></div>
    </div>`;
  document.getElementById("abLog").innerHTML = d.recent.length
    ? d.recent.map(autobetRow).join("")
    : `<div class="empty">No auto-bets yet. Arm it (paper mode) and it'll fire on the model's strongest value picks.</div>`;
}
function autobetRow(b) {
  const failed = (b.status || "").includes("failed");
  const ok = !failed && (b.mode !== "live" || b.status.includes("✓"));
  return `<div class="bet">
    <div class="bet-info"><b>${b.selection} <span class="muted">(${b.home} v ${b.away})</span></b>
      <div class="meta">$${b.stake} @ ${b.odds} · edge +${(b.edge*100).toFixed(0)}% · ${new Date(b.ts*1000).toLocaleString()}</div>
      ${b.info ? `<div class="sub" style="color:${failed ? 'var(--red)' : 'var(--muted)'}">↳ ${b.info}</div>` : ""}</div>
    <span class="result-badge ${ok ? 'won' : 'lost'}">${b.status}</span></div>`;
}
document.getElementById("abSave").addEventListener("click", async () => {
  const num = (id, fallback) => { const v = parseFloat(document.getElementById(id).value); return isNaN(v) ? fallback : v; };
  const body = {
    enabled: document.getElementById("abEnabled").value === "true",
    mode: document.getElementById("abMode").value,
    max_stake: num("abMaxStake", 2),
    daily_cap: num("abDailyCap", 10),
    min_edge: num("abMinEdge", 6) / 100,
    max_bets_day: Math.round(num("abMaxBets", 3)),
  };
  const btn = document.getElementById("abSave");
  btn.disabled = true; btn.textContent = "Saving…";
  try {
    const res = await fetch(`${API}/api/autobet`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!res.ok) { alert(`Couldn't save (${res.status}). ${res.status === 401 ? "Log in again." : ""}`); return; }
    const saved = await res.json();
    btn.textContent = "✓ Saved";
    await loadAutobet();
  } catch (e) {
    alert("Save failed — network error. " + e.message);
  } finally {
    btn.disabled = false;
    setTimeout(() => { btn.textContent = "Save settings"; }, 1500);
  }
});
document.getElementById("abTestPush").addEventListener("click", async () => {
  const d = await (await fetch(`${API}/api/notify/test`, { method: "POST" })).json();
  alert(d.sent ? `Test sent to topic:\n${d.topic}\n\nIf your phone didn't buzz, subscribe to that topic in the ntfy app.` : "Couldn't send — notifications may be off.");
});
document.getElementById("abVerify").addEventListener("click", async () => {
  const out = document.getElementById("abVerifyOut");
  out.textContent = "Checking…";
  const d = await (await fetch(`${API}/api/autobet/verify`, { method: "POST" })).json();
  out.innerHTML = d.ok
    ? `✅ Connected to Kalshi — your balance is <b>$${d.result.balance_usd}</b>. Key works (no order placed).`
    : `❌ ${d.error || (d.result && d.result) || "couldn't connect"}`;
});
document.getElementById("abTestOrder").addEventListener("click", async () => {
  const out = document.getElementById("abVerifyOut");
  if (!confirm("This places ONE real ~$1 order on the most liquid market to prove live trading works. Continue?")) return;
  out.textContent = "Placing one real test order…";
  let d;
  try { d = await (await fetch(`${API}/api/autobet/test-live-order`, { method: "POST" })).json(); }
  catch (e) { out.textContent = "Network error: " + e.message; return; }
  out.innerHTML = d.ok
    ? `✅ <b>LIVE ORDER FILLED</b> — ${d.result} (${d.market} @ ${d.price_cents}¢). Live trading works.`
    : `❌ Live order failed: ${d.error || d.result || "unknown"}. (No money moved.)`;
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

/* ---------- guided leg builder (dropdowns) ---------- */
let _marketsCache = {};
async function loadComboGames() {
  const sel = document.getElementById("lbGame");
  if (sel.options.length > 1) return; // already loaded
  const d = await (await fetch(`${API}/api/matches`)).json();
  const up = d.matches.filter((m) => m.status !== "completed");
  sel.innerHTML = `<option value="">1. choose game…</option>` +
    up.map((m) => `<option data-home="${m.home}" data-away="${m.away}">${m.home} v ${m.away}</option>`).join("");
}
document.getElementById("lbGame").addEventListener("change", async (e) => {
  const opt = e.target.selectedOptions[0];
  const home = opt.dataset.home, away = opt.dataset.away;
  const mSel = document.getElementById("lbMarket"), sSel = document.getElementById("lbSel");
  sSel.innerHTML = `<option value="">3. pick…</option>`;
  if (!home) { mSel.innerHTML = `<option value="">2. market…</option>`; return; }
  mSel.innerHTML = `<option>loading…</option>`;
  const key = `${home}|${away}`;
  if (!_marketsCache[key]) {
    _marketsCache[key] = (await (await fetch(`${API}/api/markets?team_a=${encodeURIComponent(home)}&team_b=${encodeURIComponent(away)}`)).json()).markets;
  }
  mSel.dataset.home = home; mSel.dataset.away = away;
  mSel.innerHTML = `<option value="">2. market…</option>` +
    Object.keys(_marketsCache[key]).map((c) => `<option>${c}</option>`).join("");
});
document.getElementById("lbMarket").addEventListener("change", (e) => {
  const cat = e.target.value, home = e.target.dataset.home, away = e.target.dataset.away;
  const sSel = document.getElementById("lbSel");
  const sels = (_marketsCache[`${home}|${away}`] || {})[cat] || [];
  sSel.innerHTML = `<option value="">3. pick…</option>` +
    sels.map((s, i) => `<option value="${i}">${s.label} — ${(s.prob*100).toFixed(0)}% (odds ${s.fair_odds})</option>`).join("");
});
document.getElementById("lbAdd").addEventListener("click", () => {
  const gOpt = document.getElementById("lbGame").selectedOptions[0];
  const home = gOpt && gOpt.dataset.home, away = gOpt && gOpt.dataset.away;
  const cat = document.getElementById("lbMarket").value;
  const sIdx = document.getElementById("lbSel").value;
  if (!home || !cat || sIdx === "") return alert("Pick a game, a market, and a selection first.");
  const s = _marketsCache[`${home}|${away}`][cat][parseInt(sIdx)];
  addComboLeg(`${home} v ${away}: ${s.label}`, s.prob, s.fair_odds, home, away, cat, s.label);
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
document.getElementById("mbAdd").addEventListener("click", async () => {
  const desc = document.getElementById("mbDesc").value.trim();
  if (!desc) return alert("Describe the bet first.");
  const body = {
    description: desc,
    stake: parseFloat(document.getElementById("mbStake").value) || 0,
    odds: parseFloat(document.getElementById("mbOdds").value) || 0,
    hit: document.getElementById("mbResult").value === "hit",
  };
  const btn = document.getElementById("mbAdd");
  btn.disabled = true; btn.textContent = "Adding…";
  try {
    const res = await fetch(`${API}/api/bets/manual`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!res.ok) { alert(`Couldn't add (${res.status}).`); return; }
    document.getElementById("mbDesc").value = "";
    document.getElementById("manualBetBox").open = false;
    await loadRecord();
  } finally { btn.disabled = false; btn.textContent = "Add to record"; }
});

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
    document.getElementById(`del-${b.id}`).onclick = () => removeBet(b.id);
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
      <button id="del-${b.id}" class="btn del" title="Remove / changed my mind">✕</button>
    </div></div>`;
}

async function removeBet(id) {
  if (!confirm("Remove this bet from your record?\n\n(Use this if you changed your mind or didn't actually place it. It won't count toward your stats.)")) return;
  await fetch(`${API}/api/combo/${id}`, { method: "DELETE" });
  loadRecord();
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
    const legs = parseInt(document.getElementById("abLegs").value, 10) || 3;
    const data = await (await fetch(`${API}/api/matches`)).json();
    // All styles (incl. Best Parlay) use only the games on the selected date.
    const games = data.matches
      .filter((m) => m.status !== "completed" && (!dateVal || localDate(m.commence_time) === dateVal))
      .slice(0, 12).map((m) => ({ home: m.home, away: m.away }));
    if (!games.length) return alert("No games found for that date. Try another day or the All filter on the Live Board.");
    const r = await (await fetch(`${API}/api/parlay/auto`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ games, style, legs }),
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
