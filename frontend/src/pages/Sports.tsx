import { useState } from "react";
import { useSearchParams } from "react-router";
import { Panel } from "../components/Panel";
import { useApi, type ApiState, type PanelStatus } from "../lib/api";

export interface Side { id: string; abbr: string; name: string; short: string; score: number | null;
  record: string | null; rank: number | null; elo?: number }
export interface Probs { home: number | null; away: number | null; draw: number | null }
export interface SlateGame { id: string; start: number; state: "pre" | "in" | "post"; detail: string; neutral: boolean;
  completed: boolean; home: Side; away: Side; elo: Probs;
  book: (Probs & { detail: string | null; provider: string | null }) | null; gap: number | null }
export interface RatingRow { pos: number; id: string; abbr: string; name: string; short: string; rank: number | null;
  elo: number; w: number; l: number; d?: number; pts?: number; form: string; change_7d: number }
export interface Record_ { n: number; hit_rate: number | null; logloss: number | null; brier: number | null;
  vs_book: { n: number; elo_logloss: number | null; book_logloss: number | null; elo_brier: number | null; book_brier: number | null } }
export interface LeagueBlob { ready: boolean; key: string; label: string; built_at?: number; source?: string;
  season?: number; season_name?: string | null; status?: "in_season" | "offseason";
  backfill?: { done: number; total: number }; slate?: SlateGame[]; game_of_the_day?: string | null;
  ratings?: RatingRow[]; record?: Record_; params?: { k: number; hfa: number; regress: number }; errors?: string[] }
export interface IndexLeague { key: string; label: string; status: string; live: number; games: number;
  built_at: number | null; errors: string[] }
export interface SportsIndex { ready: boolean; built_at?: number; leagues: IndexLeague[] }

const pct = (p: number | null | undefined) => (p == null ? "—" : `${Math.round(p * 100)}%`);
const when = (t: number) => new Date(t * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
const team = (s: Side) => <>{s.rank != null && <span className="muted small">{s.rank} </span>}<span className="sp-team">{s.short || s.abbr}</span></>;
const UNPROVEN = "Elo rating model · unproven, not a recommendation";

function status(state: ApiState<LeagueBlob>): PanelStatus {
  return state.data && !state.data.ready ? "LOADING" : state.status;
}

function Matchup({ g, draws }: { g: SlateGame; draws: boolean }) {
  const score = g.state !== "pre" && g.home.score != null;
  return (
    <tr className={g.state === "post" ? "done" : undefined}>
      <td className="num muted small">{g.state === "pre" ? when(g.start) : g.detail}
        {g.state === "in" && <span className="chip chip-live sp-live">LIVE</span>}</td>
      <td>{team(g.away)}{score && <b className="num"> {g.away.score}</b>}
        <span className="muted"> {g.neutral ? "vs" : "@"} </span>
        {team(g.home)}{score && <b className="num"> {g.home.score}</b>}</td>
      <td className="num r">{pct(g.elo.home)}{draws && <span className="muted small"> D {pct(g.elo.draw)}</span>}</td>
      <td className="num r">{g.book ? pct(g.book.home) : "—"}</td>
      <td className={`num r ${g.gap == null ? "" : Math.abs(g.gap) >= 0.08 ? "warn" : "muted"}`}>
        {g.gap == null ? "" : `${g.gap > 0 ? "+" : ""}${Math.round(g.gap * 100)}`}</td>
    </tr>
  );
}

const dayKey = (t: number) => new Date(t * 1000).toDateString();

function Slate({ state, draws }: { state: ApiState<LeagueBlob>; draws: boolean }) {
  const [more, setMore] = useState(false);
  const b = state.data;
  const all = b?.slate ?? [];
  // Live games, then the first day that has games; the rest of the window behind a button.
  const first = all.find((g) => g.state !== "in");
  const head = all.filter((g) => g.state === "in" || (first && dayKey(g.start) === dayKey(first.start)));
  const games = more ? all : head;
  return (
    <Panel title="Scores & slate" source={`${b?.source ?? "ESPN"} · lines: sportsbook via ESPN`} status={status(state)}
      asOf={state.asOf} error={state.error} className="sp-wide">
      {games.length === 0 ? <p className="muted">{b?.status === "offseason" ? "Offseason: no games scheduled." : "No games in the next few days."}</p> : (
        <table className="table"><thead><tr><th>When</th><th>Matchup</th>
          <th className="r" title="Home win, from our Elo ratings, before the game">Elo home</th>
          <th className="r" title="Home win implied by the sportsbook moneyline, vig removed">Book home</th>
          <th className="r" title="Elo minus book, in points">Gap</th></tr></thead>
          <tbody>{games.map((g) => <Matchup key={g.id} g={g} draws={draws} />)}</tbody></table>)}
      {all.length > head.length && <button type="button" className="link-btn" onClick={() => setMore(!more)}>
        {more ? "Today only" : `Next few days (${all.length - head.length} more)`}</button>}
      <p className="footnote">{UNPROVEN}. Home % shown; live games keep their pregame number. Gap ≥ 8 points is highlighted
        because Elo knows nothing about injuries or rest; it is not an edge.</p>
    </Panel>
  );
}

function GameOfDay({ state, draws }: { state: ApiState<LeagueBlob>; draws: boolean }) {
  const b = state.data;
  const g = b?.slate?.find((x) => x.id === b.game_of_the_day);
  return (
    <Panel title="Game of the day" source="strongest close matchup by Elo" status={status(state)} asOf={state.asOf} error={state.error}>
      {!g ? <p className="muted">No game today.</p> : (<>
        <p className="big-figure"><b>{g.away.short} {g.neutral ? "vs" : "@"} {g.home.short}</b></p>
        <p className="muted small">{g.state === "pre" ? when(g.start) : g.detail}
          {g.state !== "pre" && g.home.score != null && <> · {g.away.score}–{g.home.score}</>}</p>
        <dl className="kv">
          <dt>Elo</dt><dd className="num">{g.home.abbr} {pct(g.elo.home)}{draws && ` · D ${pct(g.elo.draw)}`} · {g.away.abbr} {pct(g.elo.away)}</dd>
          <dt>Book</dt><dd className="num">{g.book ? `${g.home.abbr} ${pct(g.book.home)} · ${g.away.abbr} ${pct(g.book.away)}` : "no line"}</dd>
          <dt>Ratings</dt><dd className="num">{g.home.abbr} {g.home.elo?.toFixed(0)} · {g.away.abbr} {g.away.elo?.toFixed(0)}</dd>
          {g.book?.detail && <><dt>Line</dt><dd className="num">{g.book.detail}</dd></>}
        </dl></>)}
    </Panel>
  );
}

function Ratings({ state, draws }: { state: ApiState<LeagueBlob>; draws: boolean }) {
  const [all, setAll] = useState(false);
  const rows = state.data?.ratings ?? [];
  const shown = all ? rows : rows.slice(0, 25);
  return (
    <Panel title="Power ratings" source="our Elo, from every final this season and last" status={status(state)}
      asOf={state.asOf} error={state.error}>
      {rows.length === 0 ? <p className="muted">No finals yet.</p> : (<>
        <table className="table"><thead><tr><th className="r">#</th><th>Team</th><th className="r">Elo</th>
          <th className="r">{draws ? "W-D-L" : "W-L"}</th>{draws && <th className="r">Pts</th>}<th>Form</th><th className="r">7d</th></tr></thead>
          <tbody>{shown.map((r) => (
            <tr key={r.id}><td className="num r muted">{r.pos}</td>
              <td>{r.rank != null && <span className="muted small">{r.rank} </span>}{r.short}</td>
              <td className="num r">{r.elo.toFixed(0)}</td>
              <td className="num r">{draws ? `${r.w}-${r.d}-${r.l}` : `${r.w}-${r.l}`}</td>
              {draws && <td className="num r">{r.pts}</td>}
              <td className="num small">{r.form}</td>
              <td className={`num r ${r.change_7d > 0 ? "good" : r.change_7d < 0 ? "bad" : "muted"}`}>
                {r.change_7d === 0 ? "·" : `${r.change_7d > 0 ? "+" : ""}${r.change_7d.toFixed(0)}`}</td></tr>))}</tbody></table>
        {rows.length > 25 && <button type="button" className="link-btn" onClick={() => setAll(!all)}>
          {all ? "Top 25" : `Show all ${rows.length}`}</button>}</>)}
    </Panel>
  );
}

function ModelRecord({ state }: { state: ApiState<LeagueBlob> }) {
  const b = state.data;
  const r = b?.record;
  const vb = r?.vs_book;
  const bf = b?.backfill;
  return (
    <Panel title="Elo track record" source="pregame forecasts vs results, this season" status={status(state)}
      asOf={state.asOf} error={state.error}>
      {r && (<>
        <p className="big-figure"><b className="num">{pct(r.hit_rate)}</b><span className="muted">picked right over {r.n} games</span></p>
        <dl className="kv">
          <dt>Log loss</dt><dd className="num">{r.logloss?.toFixed(3) ?? "—"}</dd>
          <dt>Brier</dt><dd className="num">{r.brier?.toFixed(3) ?? "—"}</dd>
          <dt>Evidence</dt><dd><span className="kind kind-print">WATCHING</span></dd>
        </dl>
        {vb && vb.n > 0 ? (
          <p className={vb.elo_logloss! < vb.book_logloss! ? "good" : "bad"}>
            Same {vb.n} games vs the book: Elo log loss {vb.elo_logloss!.toFixed(3)}, book {vb.book_logloss!.toFixed(3)}
            {vb.elo_logloss! < vb.book_logloss! ? " (Elo better so far)" : " (book better)"}.</p>
        ) : <p className="muted">No pregame lines captured yet. Collecting them from now on, so Elo can be scored against the market.</p>}
        <p className="footnote">K {b!.params?.k} · home edge {b!.params?.hfa} · {Math.round((b!.params?.regress ?? 0) * 100)}% pulled to average each season.
          Hit rate is flattered by mismatches; log loss against the book is the number that matters.</p>
        {bf && bf.done < bf.total && <p className="footnote warn">Loading history: {bf.done}/{bf.total} {b?.key === "nfl" || b?.key === "ncaaf" ? "weeks" : "days"}.
          Ratings firm up as it finishes.</p>}
      </>)}
    </Panel>
  );
}

function League({ league }: { league: string }) {
  const state = useApi<LeagueBlob>(`/sports/${league}`, { maxAgeSec: 1200, asOf: (d) => d.built_at ?? null, refreshSec: 60 });
  const draws = league === "epl";
  const errs = state.data?.errors ?? [];
  return (
    <div className="grid sports-grid">
      <Slate state={state} draws={draws} />
      <GameOfDay state={state} draws={draws} />
      <Ratings state={state} draws={draws} />
      <ModelRecord state={state} />
      {errs.length > 0 && <p className="footnote bad sp-wide">Last refresh: {errs.join("; ")}</p>}
    </div>
  );
}

export function Sports() {
  const idx = useApi<SportsIndex>("/sports", { maxAgeSec: 1200, asOf: (d) => d.built_at ?? null, refreshSec: 120 });
  const [params, setParams] = useSearchParams();
  const leagues = idx.data?.leagues ?? [];
  const fallback = leagues.find((l) => l.status === "in_season" && l.live > 0) ?? leagues.find((l) => l.status === "in_season") ?? leagues[0];
  const current = params.get("league") ?? fallback?.key;
  if (idx.status === "ERROR" && !idx.data) {
    return <div className="grid"><Panel title="Sports" source="/api/sports" status="ERROR" asOf={null} error={idx.error} /></div>;
  }
  if (!idx.data || !idx.data.ready || !current) {
    return <div className="grid"><Panel title="Sports" source="ESPN refresh job" status={idx.data ? "STALE" : "LOADING"} asOf={null}>
      <p>No sports data yet. The refresh job builds it within a few minutes of a deploy.</p></Panel></div>;
  }
  return (
    <>
      <nav className="sp-tabs" aria-label="Leagues">
        {leagues.map((l) => (
          <button key={l.key} type="button" aria-pressed={l.key === current}
            className={`sp-tab${l.key === current ? " on" : ""}${l.status === "offseason" ? " off" : ""}`}
            onClick={() => setParams({ league: l.key })}>
            {l.label}{l.live > 0 && <span className="sp-count">{l.live}</span>}
            {l.errors.length > 0 && <span className="bad" title={l.errors.join("; ")}> !</span>}
          </button>))}
      </nav>
      <League key={current} league={current} />
    </>
  );
}
