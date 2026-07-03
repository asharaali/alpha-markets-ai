/* Alpha Markets AI — frontend logic (no build step, plain ES modules-free JS) */

const API = ""; // same origin
let comboLegs = [];
let pollTimer = null;

/* ---------- sport switch (soccer World Cup | MLB) ---------- */
let SPORT = "soccer";
const SPORTS_META = {
  soccer: { icon: "⚽", label: "World Cup", unit: "goals", winMarket: "Match Result", hasDraw: true },
  mlb: { icon: "⚾", label: "MLB", unit: "runs", winMarket: "Moneyline", hasDraw: false },
};
const meta = () => SPORTS_META[SPORT];
// append ?sport= to a GET url; add &sport= if it already has a query string
function sp(url) { return url + (url.includes("?") ? "&" : "?") + "sport=" + SPORT; }

document.querySelectorAll(".sportbtn").forEach((b) => {
  b.addEventListener("click", () => {
    if (b.dataset.sport === SPORT) return;
    document.querySelectorAll(".sportbtn").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    SPORT = b.dataset.sport;
    // reset per-sport caches + UI state
    comboLegs = []; renderComboLegs();
    _coGames = []; _comboSingles = {}; _singles = [];
    document.getElementById("lbGame").innerHTML = `<option value="">1. choose game…</option>`;
    document.getElementById("coGame").innerHTML = `<option value="">choose game…</option>`;
    document.getElementById("singleCat").innerHTML = `<option value="">All categories</option>`;
    const tl = document.getElementById("tagline");
    if (tl) tl.textContent = `Edge detection & risk-sized calls — ${meta().label} beta`;
    loadModelInfo();
    // reload whichever tab is currently open
    const active = document.querySelector(".tab.active");
    if (active) {
      const t = active.dataset.tab;
      if (t === "dashboard") loadMatches();
      else if (t === "kalshi") loadKalshi();
      else if (t === "singles") loadSingles();
      else if (t === "combo") loadComboGames();
      else if (t === "cashout") loadCashoutGames();
    } else loadMatches();
  });
});

/* ---------- tabs ---------- */
document.querySelectorAll(".tab").forEach((t) => {
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    t.classList.add("active");
    document.getElementById(t.dataset.tab).classList.add("active");
    if (typeof stopCrypto === "function") stopCrypto();   // pause crypto auto-refresh off-tab
    if (t.dataset.tab === "kalshi") loadKalshi();
    if (t.dataset.tab === "singles") loadSingles();
    if (t.dataset.tab === "crypto") startCrypto();
    if (t.dataset.tab === "record") loadRecord();
    if (t.dataset.tab === "autobet") loadAutobet();
    if (t.dataset.tab === "combo") loadComboGames();
    if (t.dataset.tab === "weather") loadWeather();
    if (t.dataset.tab === "cashout") loadCashoutGames();
  });
});

/* ---------- live Kalshi cash-out sync ---------- */
let _coGames = [];
async function loadCashoutGames() {
  if (_coGames.length) return;
  try {
    const d = await (await fetch(sp(`${API}/api/matches`))).json();
    _coGames = (d.matches || []).filter((m) => m.status !== "completed");
    const sel = document.getElementById("coGame");
    sel.innerHTML = `<option value="">choose game…</option>`
      + _coGames.map((m, i) => `<option value="${i}">${m.home} v ${m.away}</option>`).join("");
  } catch (e) {}
}
document.getElementById("coGame").addEventListener("change", (e) => {
  const m = _coGames[e.target.value];
  const s = document.getElementById("coSel");
  if (!m) { s.innerHTML = `<option value="">side…</option>`; return; }
  const sides = meta().hasDraw ? [m.home, "Draw", m.away] : [m.home, m.away];
  s.innerHTML = sides.map((x) => `<option value="${x}">${x}</option>`).join("");
});
document.getElementById("coLivePull").addEventListener("click", async () => {
  const m = _coGames[document.getElementById("coGame").value];
  const selection = document.getElementById("coSel").value;
  const entry = parseFloat(document.getElementById("coLiveEntry").value);
  const out = document.getElementById("coLiveOut");
  if (!m || !selection || !entry) return (out.textContent = "Pick a game, side, and your entry price first.");
  out.textContent = "Pulling live Kalshi price…";
  const d = await (await fetch(`${API}/api/cashout/live`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ home: m.home, away: m.away, selection, entry_price: entry, stake: parseFloat(document.getElementById("coStake").value) || 100 }),
  })).json();
  if (!d.ok) return (out.innerHTML = `❌ ${d.error}`);
  const cls = d.action.includes("CASH OUT") || d.action.includes("EXIT") ? "var(--red)" : d.action.includes("HOLD") ? "var(--green)" : "var(--text)";
  out.innerHTML = `<b style="color:${cls};font-size:15px">${d.action}</b> — ${d.reason}<br>`
    + `Live Kalshi cash-out: <b>${Math.round(d.live_cashout_price * 100)}¢</b> · you paid ${Math.round(entry * 100)}¢ · `
    + `P&L <b>${d.unrealized_pnl_pct > 0 ? "+" : ""}${d.unrealized_pnl_pct}%</b> · model fair ${Math.round(d.model_fair_value * 100)}¢ · depth $${d.book_depth}<br>`
    + `<span class="muted">${d.ticker} · ${d.source}</span>`;
  // also fill the manual fields so the big "Check position" matches
  document.getElementById("coEntry").value = entry;
  document.getElementById("coCurrent").value = d.live_cashout_price;
  document.getElementById("coModel").value = d.model_fair_value;
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
document.getElementById("abDiagnose").addEventListener("click", async () => {
  const out = document.getElementById("abDiagnoseOut");
  out.textContent = "Checking…";
  const d = await (await fetch(`${API}/api/notify/diagnose`)).json();
  const betLines = (d.your_bets || []).map((b) =>
    `• ${b.trackable ? "✅ tracked" : "⚠️ NOT tracked"} — ${b.games.join(", ") || b.legs.join(" + ")} `
    + `(${b.any_live ? "LIVE: " + b.action : "not live yet"})`).join("<br>");
  out.innerHTML = `<b>${d.verdict}</b><br>`
    + `Topic: <b>${d.topic || "none"}</b> · alerts ${d.notify_enabled ? "ON" : "OFF"} · `
    + `monitor ${d.monitor_running ? "running ✅" : "NOT running ❌"} (last ran ${d.monitor_last_ran_secs_ago}s ago)<br>`
    + `Pending bets: <b>${d.pending_bet_count}</b><br>${betLines}<br>`
    + `<span class="muted">Live games right now: ${d.live_games_now.join(", ") || "none"}</span>`;
});
document.getElementById("abPreview").addEventListener("click", async () => {
  const d = await (await fetch(`${API}/api/notify/preview`, { method: "POST" })).json();
  alert(`Fired ${d.sent}/${d.of} real game alerts to your phone (kickoff, goal, heads-up, cash-out).\n`
    + `These use the exact same code the live monitor uses during a game. If they buzz, game-time will too.`);
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
    const res = await fetch(sp(`${API}/api/matches`));
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
  const isMlb = (m.sport || SPORT) === "mlb";
  const hasDraw = !isMlb;
  const unit = isMlb ? "runs" : "goals";
  const live = m.status === "live";
  const clock = isMlb ? `${(m.live_half === "bottom" ? "Bot " : "Top ")}${m.live_inning || 1}` : `${m.live_minute || 0}'`;
  const head = live
    ? `<div class="live-tag">LIVE ${clock}</div>
       <div class="score">${m.live_score.home}–${m.live_score.away}</div>`
    : `<div class="meta">${new Date(m.commence_time).toLocaleString([], {month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"})}</div>`;

  // Market-implied probabilities (from the book's odds) so you can compare to our model.
  let marketLine = "";
  const winMkt = isMlb ? "Moneyline" : "Match Result";
  const mr = (m.suggestions || []).filter((s) => s.market === winMkt);
  if (mr.length >= 2) {
    const g = (name) => mr.find((s) => s.selection === name);
    const h = g(m.home), a = g(m.away), dr = g("Draw");
    if (h && a) {
      marketLine = `<div class="marketline">Market (book/Kalshi) · ${m.home} ${pct(h.market_prob)}`
        + (hasDraw && dr ? ` · Draw ${pct(dr.market_prob)}` : "")
        + ` · ${m.away} ${pct(a.market_prob)}</div>`;
    }
  }

  // Probable starting pitchers + bullpen (MLB) — the biggest driver of the matchup.
  let pitchersLine = "";
  if (isMlb && (m.sp_home_name || m.sp_away_name)) {
    // bullpen mult < 1 = allows fewer runs = better; show + when the pen is a strength.
    const pen = (b) => b != null && Math.abs(b - 1) > 0.02
      ? ` <span class="muted" title="team bullpen vs league avg run prevention">pen ${b < 1 ? "+" : ""}${((1 - b) * 100).toFixed(0)}%</span>` : "";
    const fmt = (name, era, label, b) => name
      ? `${name}${era != null ? ` <span class="muted">${era} ERA</span>` : ""} <span class="sp-tag ${label}">${label}</span>${pen(b)}`
      : `<span class="muted">TBD</span>`;
    pitchersLine = `<div class="pitchers">⚾ Starters · ${fmt(m.sp_away_name, m.sp_away_era, m.sp_away_label, m.sp_away_bullpen)}`
      + ` <span class="muted">@</span> ${fmt(m.sp_home_name, m.sp_home_era, m.sp_home_label, m.sp_home_bullpen)}</div>`;
  }

  // Live win-probability shift vs pre-game (so you can see momentum swing).
  let liveShift = "";
  if (live && m.model.pregame_probs) {
    const pre = m.model.pregame_probs;
    const dH = (p.home - pre.home) * 100;
    const exp = m.model.expected_final_runs || m.model.expected_final_goals;
    const arrow = (d) => d > 1 ? `<span class="up">▲${d.toFixed(0)}</span>` : d < -1 ? `<span class="down">▼${Math.abs(d).toFixed(0)}</span>` : "";
    liveShift = `<div class="live-shift">live vs pre-game: ${m.home} ${arrow(dH)} · exp final ${exp.a}–${exp.b} ${unit}</div>`;
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
      ${hasDraw ? `<span class="d" style="width:${p.draw*100}%"></span>` : ""}
      <span class="a" style="width:${p.away*100}%"></span>
    </div>
    <div class="problabels"><span><b>Our model</b> · ${m.home} ${pct(p.home)}</span>${hasDraw ? `<span>Draw ${pct(p.draw)}</span>` : ""}<span>${m.away} ${pct(p.away)}</span></div>
    ${pitchersLine}
    ${marketLine}
    ${liveShift}
    <div class="sugg">${rows}</div>
    <button class="markets-btn" data-home="${m.home}" data-away="${m.away}">＋ all markets — ${isMlb ? "run line, totals, team totals, F5" : "goals, scores, player props"} ▾</button>
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
  const d = await (await fetch(sp(`${API}/api/markets?team_a=${encodeURIComponent(home)}&team_b=${encodeURIComponent(away)}`))).json();
  // These are the model's FAIR prices (odds = 1/prob), not tradeable book prices — reference
  // only. To build a combo, use the Single Bets tab / combo builder, which carry live Kalshi odds.
  box.innerHTML = `<div class="mk-note sub" style="padding:6px 2px;opacity:.8">📊 Model fair prices — reference only (not tradeable). Build combos from the <b>Single Bets</b> tab for real Kalshi odds.</div>`
    + Object.entries(d.markets).map(([cat, sels]) => `
    <div class="mk-cat">${cat}</div>
    ${sels.map((s) => `
      <div class="mk-row">
        <span>${s.label}</span>
        <span class="mk-prob">${(s.prob*100).toFixed(0)}%</span>
        <span class="mk-odds" title="model fair odds (1/prob), not a book price">${s.fair_odds}</span>
      </div>`).join("")}
  `).join("");
}

document.getElementById("matches").addEventListener("click", (e) => {
  const btn = e.target.closest(".markets-btn");
  if (btn) toggleMarkets(btn);
});

/* ---------- guided leg builder (dropdowns) ----------
   Sourced from the REAL-priced Kalshi singles (live odds + tickers), not the model's fair
   odds — so every leg you add carries a tradeable price and can actually be placed. */
let _comboSingles = {};   // "home|away" -> { bet_type -> [single, ...] }
async function loadComboGames() {
  const sel = document.getElementById("lbGame");
  if (sel.options.length > 1) return; // already loaded
  const d = await (await fetch(sp(`${API}/api/kalshi/singles`))).json();
  _comboSingles = {};
  (d.bets || []).forEach((b) => {
    if (!(b.kalshi_price_cents > 0)) return;      // untraded -> no real price
    if (b.confidence === "reference") return;     // props are reference-only, never a combo leg
    const gk = `${b.home}|${b.away}`;
    (_comboSingles[gk] = _comboSingles[gk] || {});
    (_comboSingles[gk][b.bet_type] = _comboSingles[gk][b.bet_type] || []).push(b);
  });
  const games = Object.keys(_comboSingles);
  sel.innerHTML = games.length
    ? `<option value="">1. choose game…</option>` +
        games.map((gk) => { const [h, a] = gk.split("|"); return `<option data-home="${h}" data-away="${a}">${h} v ${a}</option>`; }).join("")
    : `<option value="">no live Kalshi markets right now</option>`;
}
document.getElementById("lbGame").addEventListener("change", (e) => {
  const opt = e.target.selectedOptions[0];
  const home = opt.dataset.home, away = opt.dataset.away;
  const mSel = document.getElementById("lbMarket"), sSel = document.getElementById("lbSel");
  sSel.innerHTML = `<option value="">3. pick…</option>`;
  if (!home) { mSel.innerHTML = `<option value="">2. market…</option>`; return; }
  mSel.dataset.home = home; mSel.dataset.away = away;
  const markets = _comboSingles[`${home}|${away}`] || {};
  mSel.innerHTML = `<option value="">2. market…</option>` +
    Object.keys(markets).map((c) => `<option>${c}</option>`).join("");
});
document.getElementById("lbMarket").addEventListener("change", (e) => {
  const cat = e.target.value, home = e.target.dataset.home, away = e.target.dataset.away;
  const sSel = document.getElementById("lbSel");
  const sels = (_comboSingles[`${home}|${away}`] || {})[cat] || [];
  sSel.innerHTML = `<option value="">3. pick…</option>` +
    sels.map((s, i) => `<option value="${i}">${s.selection} — ${s.kalshi_price_cents}¢ · model ${(s.fair_prob*100).toFixed(0)}%${s.value_bet ? " ★" : ""}</option>`).join("");
});
document.getElementById("lbAdd").addEventListener("click", () => {
  const gOpt = document.getElementById("lbGame").selectedOptions[0];
  const home = gOpt && gOpt.dataset.home, away = gOpt && gOpt.dataset.away;
  const cat = document.getElementById("lbMarket").value;
  const sIdx = document.getElementById("lbSel").value;
  if (!home || !cat || sIdx === "") return alert("Pick a game, a market, and a selection first.");
  addComboLegFromSingle(_comboSingles[`${home}|${away}`][cat][parseInt(sIdx)]);
});

/* ---------- combo builder ---------- */
function addLegFromBtn(b) {
  const d = b.dataset;
  addComboLeg(d.label, parseFloat(d.prob), parseFloat(d.odds), d.home, d.away, d.market, d.sel, d.ticker || null, d.side || "yes");
}
function addComboLeg(label, prob, odds, home, away, market, selection, ticker, side) {
  comboLegs.push({ label, model_prob: prob, market_odds_decimal: odds,
    home: home || null, away: away || null, market: market || null, selection: selection || null,
    // Real Kalshi ticker + side (from the singles board) so the combo can actually be placed.
    kalshi_ticker: ticker || null, side: side || "yes" });
  renderComboLegs();
  // jump to combo tab
  document.querySelector('.tab[data-tab="combo"]').click();
}
// Build a combo leg from a real-priced Kalshi single (tradeable odds + ticker + side).
function addComboLegFromSingle(b) {
  const label = (b.selection || "").includes(" v ") ? b.selection : `${b.home} v ${b.away}: ${b.selection}`;
  const odds = b.kalshi_price_cents > 0 ? 100 / b.kalshi_price_cents : 99;
  addComboLeg(label, b.fair_prob, odds, b.home, b.away, b.bet_type, b.selection, b.ticker, b.side || "yes");
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
    body: JSON.stringify({ legs: comboLegs, sport: SPORT }),
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
    <button id="logBet" class="btn primary" style="margin-top:12px">＋ Log this bet to My Record</button>
    <div style="margin-top:8px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <button id="placeKalshi" class="btn primary">🤖 Place combo on Kalshi</button>
      <label class="sub" style="display:flex;gap:6px;align-items:center;margin:0">
        <input type="checkbox" id="placeLive"> place LIVE (real money)</label>
    </div>
    <p class="sub" id="placeMsg" style="margin-top:6px"></p>`;
  document.getElementById("logBet").addEventListener("click", logCurrentCombo);
  document.getElementById("placeKalshi").addEventListener("click", placeComboOnKalshi);
});

async function placeComboOnKalshi() {
  if (!lastCombo) return;
  const amount = parseFloat(document.getElementById("comboStake").value) || 0;
  if (amount <= 0) return alert("Enter your amount in the stake box first.");
  const live = document.getElementById("placeLive").checked;
  if (live && !confirm(`Place this combo LIVE on Kalshi for $${amount} of REAL money?`)) return;
  const btn = document.getElementById("placeKalshi");
  btn.disabled = true; btn.textContent = "Placing…";
  try {
    const r = await (await fetch(`${API}/api/combo/place`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ combo: lastCombo, amount, mode: live ? "live" : "paper", book: "Kalshi" }),
    })).json();
    if (r.error) { document.getElementById("placeMsg").textContent = "⚠ " + r.error; btn.disabled = false; btn.textContent = "🤖 Place combo on Kalshi"; return; }
    btn.textContent = r.mode === "live" ? "✓ Placed LIVE — tracking it" : "✓ Placed (paper) — tracking it";
    document.getElementById("placeMsg").textContent = r.note + " I'll alert you to cash out if it turns.";
    loadRecord();
  } catch (e) { document.getElementById("placeMsg").textContent = "⚠ Couldn't place: " + e; btn.disabled = false; btn.textContent = "🤖 Place combo on Kalshi"; }
}

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
    const d = await (await fetch(sp(`${API}/api/kalshi`))).json();
    // MLB returns a flat list of priced moneyline singles; soccer returns per-game cards.
    if (d.sport === "mlb") {
      const bets = d.singles || [];
      if (!bets.length) { wrap.innerHTML = `<div class="empty">No live Kalshi MLB game prices right now.</div>`; return; }
      wrap.innerHTML = bets.map(kalshiMlbCard).join("");
      return;
    }
    if (!d.games.length) { wrap.innerHTML = `<div class="empty">No Kalshi World Cup markets open.</div>`; return; }
    const note = d.tradeable === 0
      ? `<div class="empty" style="grid-column:1/-1">${d.count} World Cup events found, but Kalshi has no live prices on them yet (untraded order books). They'll populate as games get liquidity — the model overlay is ready.</div>` : "";
    wrap.innerHTML = note + d.games.map(kalshiCard).join("");
  } catch (e) {
    wrap.innerHTML = `<div class="empty">Couldn't reach Kalshi.</div>`;
  }
}

function kalshiMlbCard(b) {
  const star = b.value_bet ? `<span class="star">★ VALUE</span>` : "";
  const bookTxt = b.book_prob != null ? `book ${(b.book_prob*100).toFixed(0)}%` : "model-only";
  return `<div class="card ${b.value_bet ? "" : "dim"}">
    <div class="card-head"><div class="teams">${b.selection} ${star}</div>
      <div class="meta">${b.away} @ ${b.home}</div></div>
    <div class="sugg"><div class="sugg-row">
      <span class="name"><span class="tier-dot ${b.tier}"></span>${b.kalshi_price_cents}¢</span>
      <span class="odds">model ${(b.model_prob*100).toFixed(0)}% <small>${bookTxt}</small></span>
      <span class="ev ${b.ev_per_dollar>0?'pos':'neg'}">${b.ev_per_dollar>0?"+":""}${(b.ev_per_dollar*100).toFixed(1)}%</span>
    </div></div></div>`;
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

/* ---------- Single Bets ---------- */
let _singles = [];
async function loadSingles() {
  const wrap = document.getElementById("singlesList");
  wrap.innerHTML = `<div class="empty">Loading every Kalshi market + cross-checking the books…</div>`;
  try {
    const d = await (await fetch(sp(`${API}/api/kalshi/singles`))).json();
    _singles = d.bets || [];
    const sel = document.getElementById("singleCat");
    if (sel.options.length <= 1)
      sel.innerHTML = `<option value="">All categories</option>`
        + (d.categories || []).map((c) => `<option value="${c}">${c}</option>`).join("");
    renderSingles();
  } catch (e) {
    wrap.innerHTML = `<div class="empty">Couldn't load single bets.</div>`;
  }
}

function renderSingles() {
  const cat = document.getElementById("singleCat").value;
  const valueOnly = document.getElementById("singleValueOnly").checked;
  let rows = _singles.filter((b) => (!cat || b.category === cat) && (!valueOnly || b.value_bet));
  const wrap = document.getElementById("singlesList");
  if (!rows.length) { wrap.innerHTML = `<div class="empty">No markets match. Try the All filter or refresh.</div>`; return; }
  wrap.innerHTML = rows.slice(0, 80).map(singleCard).join("");
  rows.slice(0, 80).forEach((b) => {
    const uid = _rowId(b);
    const el = document.getElementById(`place-${uid}`);
    if (el) el.onclick = () => placeSingle(b);
    const rb = document.getElementById(`research-btn-${uid}`);
    if (rb) rb.onclick = () => researchSingle(b);
    const cb = document.getElementById(`combo-${uid}`);
    if (cb) cb.onclick = () => addComboLegFromSingle(b);
  });
}

function _safeId(t) { return t.replace(/[^a-zA-Z0-9]/g, ""); }
// Unique per row: a two-sided market (e.g. BTTS) shows Yes + No rows that share one ticker,
// so element IDs must include the side or they'd collide and mis-wire the buttons.
function _rowId(b) { return _safeId(`${b.ticker}-${b.side || "yes"}`); }

async function researchSingle(b) {
  const panel = document.getElementById(`research-${_rowId(b)}`);
  panel.style.display = "block";
  panel.innerHTML = `<div class="sub">🔎 Pulling recent news…</div>`;
  const player = b.bet_type === "Goalscorer" ? b.selection.replace(/\s+\d+\+.*/, "").trim() : "";
  const q = new URLSearchParams({ home: b.home, away: b.away, ...(player ? { player } : {}) });
  try {
    const d = await (await fetch(`${API}/api/research?${q}`)).json();
    const items = (d.headlines || []).map((h) =>
      `<div class="sugg-row"><span class="name">${h.risk ? "⚠️ " : ""}${h.title}</span>
       <span class="odds"><small>${h.source} · ${h.age_hours}h</small></span></div>`).join("");
    panel.innerHTML = `<p class="sub" style="margin:6px 0">${d.summary}</p>${items || `<div class="sub">No recent coverage.</div>`}`;
  } catch (e) { panel.innerHTML = `<div class="sub">Couldn't load research.</div>`; }
}

function confBadge(b) {
  if (b.confidence === "high") return `<span class="result-badge won" title="${b.sources}">✅ books agree</span>`;
  if (b.confidence === "medium") return `<span class="result-badge" style="background:var(--amber)" title="${b.sources}">⚠️ books disagree</span>`;
  if (b.confidence === "reference") return `<span class="result-badge" style="background:#4a3a6b" title="${b.sources}">📊 reference — not a value call</span>`;
  return `<span class="result-badge" style="background:#3a4a6b" title="${b.sources}">🔵 model-only</span>`;
}

function singleCard(b) {
  const bookTxt = b.book_prob != null ? `books ${(b.book_prob*100).toFixed(0)}%` : "no book line";
  const star = b.value_bet ? `<span class="star">★ VALUE</span>` : "";
  const uid = _rowId(b);
  return `<div class="card ${b.value_bet ? "" : "dim"}">
    <div class="card-head">
      <div class="teams">${b.selection} ${star}</div>
      <div class="meta">${b.category} · ${b.bet_type}</div>
    </div>
    <div class="sugg">
      <div class="sugg-row">
        <span class="name">${b.home} v ${b.away}</span>
        <span class="odds">${b.kalshi_price_cents}¢ <small>model ${(b.model_prob*100).toFixed(0)}% · ${bookTxt}</small></span>
        <span class="ev ${b.ev_per_dollar>0?'pos':'neg'}">${b.ev_per_dollar>0?"+":""}${(b.ev_per_dollar*100).toFixed(0)}%</span>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px;gap:8px">
        ${confBadge(b)}
        <div style="display:flex;gap:6px">
          <button id="research-btn-${uid}" class="btn" title="Recent news + injury check">🔎 Research</button>
          <button id="combo-${uid}" class="btn" title="Add to combo builder (real Kalshi odds)">＋ combo</button>
          <button id="place-${uid}" class="btn ${b.value_bet?'primary':''}">Place / paper</button>
        </div>
      </div>
      <div id="research-${uid}" class="research-panel" style="display:none;margin-top:8px;border-top:1px solid var(--line,#2a3550);padding-top:8px"></div>
    </div></div>`;
}

async function placeSingle(b) {
  const amount = parseFloat(document.getElementById("singleAmount").value) || 0;
  if (amount <= 0) return alert("Enter an amount first.");
  const live = document.getElementById("singleLive").checked;
  if (live && !confirm(`Place ${b.selection} LIVE on Kalshi for $${amount} of REAL money?`)) return;
  // A single bet is a 1-leg combo carrying its exact Kalshi ticker + side (yes/no).
  const combo = { legs: [{
    label: b.selection, model_prob: b.fair_prob,
    market_odds_decimal: b.kalshi_price_cents > 0 ? 100 / b.kalshi_price_cents : 99,
    home: b.home, away: b.away, market: b.bet_type, selection: b.selection,
    kalshi_ticker: b.ticker, side: b.side || "yes",
  }], leg_count: 1, combined_model_prob: b.fair_prob, sport: SPORT,
    combined_odds_decimal: b.kalshi_price_cents > 0 ? 100 / b.kalshi_price_cents : 99,
    payout_multiple: b.kalshi_price_cents > 0 ? 100 / b.kalshi_price_cents : 99 };
  const btn = document.getElementById(`place-${_rowId(b)}`);
  btn.disabled = true; btn.textContent = "Placing…";
  try {
    const r = await (await fetch(`${API}/api/combo/place`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ combo, amount, mode: live ? "live" : "paper", book: "Kalshi" }),
    })).json();
    if (r.error) { alert(r.error); btn.disabled = false; btn.textContent = "Place / paper"; return; }
    btn.textContent = r.mode === "live" ? "✓ Placed LIVE" : "✓ Placed (paper)";
  } catch (e) { alert("Couldn't place: " + e); btn.disabled = false; btn.textContent = "Place / paper"; }
}

document.getElementById("singleCat").addEventListener("change", renderSingles);
document.getElementById("singleValueOnly").addEventListener("change", renderSingles);
document.getElementById("singleRefresh").addEventListener("click", loadSingles);

/* ---------- Crypto 15-min ---------- */
let _cryptoData = [], _cryptoPoll = null, _cryptoTick = null;
function startCrypto() {
  loadCrypto();
  clearInterval(_cryptoPoll); clearInterval(_cryptoTick);
  _cryptoPoll = setInterval(loadCrypto, 10000);   // fresh spot + prices every 10s
  _cryptoTick = setInterval(tickCrypto, 1000);    // live countdown between refreshes
}
function stopCrypto() { clearInterval(_cryptoPoll); clearInterval(_cryptoTick); _cryptoPoll = _cryptoTick = null; }

async function loadCrypto() {
  const wrap = document.getElementById("cryptoList");
  const coin = document.getElementById("cryptoCoin").value;
  try {
    const d = await (await fetch(`${API}/api/crypto${coin ? `?coin=${coin}` : ""}`)).json();
    _cryptoData = d.markets || [];
    document.getElementById("cryptoAuto").textContent = `· auto-refresh 10s · ${d.pick_count || 0} actionable`;
    renderCrypto();
  } catch (e) {
    if (!_cryptoData.length) wrap.innerHTML = `<div class="empty">Couldn't load crypto markets.</div>`;
  }
}
function renderCrypto() {
  const picksOnly = document.getElementById("cryptoValueOnly").checked;
  const rows = _cryptoData.filter((b) => !picksOnly || b.has_pick);
  const wrap = document.getElementById("cryptoList");
  if (!rows.length) { wrap.innerHTML = `<div class="empty">No open 15-min markets match right now — a fresh window opens every 15 minutes.</div>`; return; }
  wrap.innerHTML = rows.map(cryptoCard).join("");
  rows.forEach((b) => {
    const el = document.getElementById(`cplace-${_safeId(b.ticker)}`);
    if (el && b.has_pick) el.onclick = () => placeCrypto(b);
  });
}
function fmtCountdown(secs) {
  if (secs <= 0) return "closed";
  const m = Math.floor(secs / 60), s = secs % 60;
  return `${m}m ${String(s).padStart(2, "0")}s`;
}
function sideRow(coin, s, on) {
  return `<div class="sugg-row" style="${on ? 'font-weight:600' : 'opacity:.6'}">
    <span class="name">${on ? "➡ " : ""}${s.selection}</span>
    <span class="odds">${s.kalshi_price_cents}¢ <small>model ${(s.model_prob * 100).toFixed(0)}%</small></span>
    <span class="ev ${s.ev_per_dollar > 0 ? 'pos' : 'neg'}">${s.ev_per_dollar > 0 ? "+" : ""}${(s.ev_per_dollar * 100).toFixed(0)}%</span>
  </div>`;
}
function cryptoCard(b) {
  const id = _safeId(b.ticker);
  const closeMs = b.close_time ? new Date(b.close_time).getTime() : 0;
  const sig = b.signal || { direction: "flat", strength: 0, note: "" };
  const sigColor = sig.direction === "up" ? "var(--green)" : sig.direction === "down" ? "var(--red)" : "var(--muted)";
  const sigArrow = sig.direction === "up" ? "▲" : sig.direction === "down" ? "▼" : "▬";
  const bars = "█".repeat(Math.round(sig.strength * 5)) + "░".repeat(5 - Math.round(sig.strength * 5));
  const p = b.pick || { side: null };
  // The recommendation banner: what to place, or sit out.
  let rec;
  if (p.side) {
    const cc = p.confidence === "high" ? "var(--green)" : p.confidence === "medium" ? "var(--amber)" : "var(--muted)";
    rec = `<div class="action-line" style="color:${cc}">${p.action} — ${p.confidence.toUpperCase()} confidence · +${(p.ev_per_dollar * 100).toFixed(0)}% EV</div>
      <p class="sub" style="margin:4px 0 8px">${p.reason}</p>
      <button id="cplace-${id}" class="btn primary">Place the pick (buy ${p.side.toUpperCase()}) — paper unless LIVE checked</button>`;
  } else {
    rec = `<div class="action-line" style="color:var(--muted)">↔ PASS — no edge</div>
      <p class="sub" style="margin:4px 0 8px">${(b.pick && b.pick.reason) || "Fairly priced."}</p>`;
  }
  return `<div class="card ${p.side ? "" : "dim"}">
    <div class="card-head">
      <div><div class="teams">${b.coin} · target $${Number(b.strike).toLocaleString()}</div>
        <div class="meta">spot $${Number(b.spot).toLocaleString()} · vol ${(b.sigma_annual * 100).toFixed(0)}% · <span class="cd" data-close="${closeMs}">⏱ ${fmtCountdown(b.seconds_to_close)}</span></div></div>
      <div style="text-align:right;color:${sigColor}"><b>${sigArrow} ${sig.direction.toUpperCase()}</b><div class="sub" style="color:${sigColor}">${bars}</div></div>
    </div>
    <div class="marketline" style="margin:6px 0">📈 chart read: ${sig.note}</div>
    <div class="sugg">
      ${sideRow(b.coin, b.yes, p.side === "yes")}
      ${sideRow(b.coin, b.no, p.side === "no")}
    </div>
    <div style="margin-top:10px">${rec}</div>
  </div>`;
}
function tickCrypto() {
  document.querySelectorAll("#cryptoList .cd").forEach((el) => {
    const closeMs = parseInt(el.dataset.close, 10) || 0;
    el.textContent = `⏱ ${fmtCountdown(Math.round((closeMs - Date.now()) / 1000))}`;
  });
}
async function placeCrypto(b) {
  const p = b.pick;
  if (!p || !p.side) return;
  const amount = parseFloat(document.getElementById("cryptoAmount").value) || 0;
  if (amount <= 0) return alert("Enter an amount first.");
  const live = document.getElementById("cryptoLive").checked;
  if (live && !confirm(`Place ${p.selection} (buy ${p.side.toUpperCase()}) LIVE on Kalshi for $${amount} of REAL money?`)) return;
  const odds = p.price_cents > 0 ? 100 / p.price_cents : 99;
  const combo = { legs: [{
    label: p.selection, model_prob: p.model_prob, market_odds_decimal: odds,
    home: b.coin, away: "15-min", market: b.bet_type, selection: p.selection,
    kalshi_ticker: b.ticker, side: p.side,
  }], leg_count: 1, combined_model_prob: p.model_prob, sport: "crypto",
    combined_odds_decimal: odds, payout_multiple: odds };
  const btn = document.getElementById(`cplace-${_safeId(b.ticker)}`);
  btn.disabled = true; btn.textContent = "Placing…";
  try {
    const r = await (await fetch(`${API}/api/combo/place`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ combo, amount, mode: live ? "live" : "paper", book: "Kalshi" }),
    })).json();
    if (r.error) { alert(r.error); btn.disabled = false; btn.textContent = "Place the pick"; return; }
    btn.textContent = r.mode === "live" ? "✓ Placed LIVE" : "✓ Placed (paper)";
  } catch (e) { alert("Couldn't place: " + e); btn.disabled = false; btn.textContent = "Place the pick"; }
}
document.getElementById("cryptoCoin").addEventListener("change", loadCrypto);
document.getElementById("cryptoValueOnly").addEventListener("change", renderCrypto);
document.getElementById("cryptoRefresh").addEventListener("click", loadCrypto);

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
    document.getElementById(`cash-${b.id}`).onclick = () => cashoutCombo(b.id);
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
      <button id="cash-${b.id}" class="btn ${ls && ls.cash_out ? "miss" : "primary"}" title="Cash out / close this position now">💵 Cash out</button>
      <button id="hit-${b.id}" class="btn hit">Hit ✓</button>
      <button id="miss-${b.id}" class="btn miss">Miss ✗</button>
      <button id="del-${b.id}" class="btn del" title="Remove / changed my mind">✕</button>
    </div></div>`;
}

async function cashoutCombo(id) {
  if (!confirm("Cash out this combo now?\n\nThis closes the position (sells your legs on Kalshi if it was placed live) and stops tracking it. The realized value goes into your record.")) return;
  const r = await (await fetch(`${API}/api/combo/cashout`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ bet_id: id }),
  })).json();
  if (r.ok) alert(`Cashed out for ${r.cashout_value != null ? "$" + r.cashout_value : "—"} (${r.mode}).`);
  else alert(r.error || "Couldn't cash out.");
  loadRecord();
}

async function removeBet(id) {
  if (!confirm("Remove this bet from your record?\n\n(Use this if you changed your mind or didn't actually place it. It won't count toward your stats.)")) return;
  await fetch(`${API}/api/combo/${id}`, { method: "DELETE" });
  loadRecord();
}

function settledRow(b) {
  if (b.status === "cashed_out") {
    const profit = (b.cashout_value || 0) - (b.stake || 0);
    return `<div class="bet">
      <div class="bet-info"><b>${legLabels(b)}</b>
        <div class="meta">cashed out · got $${b.cashout_value} on $${b.stake} ·
          <span style="color:${profit>=0?'var(--green)':'var(--red)'}">${profit>=0?"+":""}${money(profit)}</span></div></div>
      <span class="result-badge" style="background:var(--amber)">CASHED</span></div>`;
  }
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
    const d = await (await fetch(sp(`${API}/api/model-info`))).json();
    const b = document.getElementById("modelBadge");
    if (d.trained) {
      const n = d.n_matches_trained || d.n_games_trained;
      const noun = SPORT === "mlb" ? "games" : "matches";
      b.textContent = `model ✓ ${(d.metrics.accuracy*100).toFixed(0)}% · ${d.n_teams} teams`;
      b.title = `Trained on ${n} ${noun}. Out-of-sample log loss ${d.metrics.log_loss.toFixed(3)}`
        + (d.baseline_log_loss ? ` (baseline ${d.baseline_log_loss}).` : ".");
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
    const data = await (await fetch(sp(`${API}/api/matches`))).json();
    // All styles (incl. Best Parlay) use only the games on the selected date.
    const games = data.matches
      .filter((m) => m.status !== "completed" && (!dateVal || localDate(m.commence_time) === dateVal))
      .slice(0, 12).map((m) => ({ home: m.home, away: m.away }));
    if (!games.length) return alert("No games found for that date. Try another day or the All filter on the Live Board.");
    const r = await (await fetch(`${API}/api/parlay/auto`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ games, style, legs, sport: SPORT }),
    })).json();
    if (r.error) return alert(r.error);
    comboLegs = r.legs.map((l) => ({
      label: l.label, model_prob: l.model_prob, market_odds_decimal: l.market_odds_decimal,
      home: l.home, away: l.away, market: l.market, selection: l.selection,
      // keep the Kalshi ticker + side so an auto-built combo stays placeable across every market
      kalshi_ticker: l.kalshi_ticker || null, side: l.side || "yes",
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
