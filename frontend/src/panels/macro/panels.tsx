import type { ReactNode } from "react";
import { Panel } from "../../components/Panel";
import type { ApiState, PanelStatus } from "../../lib/api";
import { oddsPct, pct0, rate, type Indicator, type Kind, type MacroBlob } from "./types";

export const KindChip = ({ kind }: { kind: Kind }) => <span className={`kind kind-${kind}`}>{kind.toUpperCase()}</span>;

type SectionKey = keyof NonNullable<MacroBlob["sections"]>;

/** One Macro panel: its own LOADING / ERROR, independent of the other sections. */
function Section({ state, section, title, source, children }: {
  state: ApiState<MacroBlob>; section: SectionKey; title: string; source: string; children: (b: MacroBlob) => ReactNode;
}) {
  const { data, status, asOf, error } = state;
  let st: PanelStatus = status, err = error;
  if (data && !data.ready) st = "LOADING";
  else if (data?.ready && !data.sections?.[section]) { st = "ERROR"; err = data.errors?.[section] ?? "No data for this section."; }
  return (
    <Panel title={title} source={source} status={st} asOf={asOf} error={err}>
      {data?.ready && data.sections?.[section] && children(data)}
      {data?.ready && data.sections?.[section] && data.errors?.[section] && <p className="footnote bad">{data.errors[section]}</p>}
    </Panel>
  );
}

const IndicatorTable = ({ items, yoy }: { items: Indicator[]; yoy?: boolean }) => (
  <table className="table"><thead><tr><th>Series</th><th className="r">{yoy ? "YoY" : "Level"}</th>
    <th className="r">{yoy ? "MoM" : "Chg"}</th><th className="r">As of</th><th /></tr></thead>
    <tbody>{items.map((i) => (
      <tr key={i.id}><td>{i.label}</td>
        <td className="num r">{yoy && i.yoy != null ? rate(i.yoy) : rate(i.value, i.value < 10 ? 2 : 1)}</td>
        <td className="num r">{yoy ? (i.mom != null ? `${i.mom >= 0 ? "+" : ""}${i.mom.toFixed(2)}%` : "—")
          : (i.change != null ? `${i.change >= 0 ? "+" : ""}${i.change.toFixed(2)}` : "—")}</td>
        <td className="num r muted">{i.as_of}</td><td><KindChip kind={i.kind} /></td></tr>
    ))}</tbody></table>
);

export function MacroPanels({ state }: { state: ApiState<MacroBlob> }) {
  return (
    <>
      <Section state={state} section="rates" title="Treasury yields" source="FRED (St. Louis Fed)">
        {(b) => <IndicatorTable items={b.sections!.rates!.items} />}
      </Section>
      <Section state={state} section="fed" title="Fed decision odds" source="Kalshi · market prices, not our forecast">
        {(b) => { const f = b.sections!.fed!; return (<>
          <p className="muted">{f.title} · today's upper bound {rate(f.current_upper)} <KindChip kind={f.kind} /></p>
          <table className="table"><tbody>{f.outcomes.map((o) => (
            <tr key={o.label}><td>{o.label}</td><td className="num r">{pct0(o.prob)}</td>
              <td><span className="bar" style={{ width: `${Math.round(o.prob * 100)}%` }} /></td></tr>))}</tbody></table>
          <a className="footnote" href={f.url} target="_blank" rel="noopener">Open on Kalshi →</a></>); }}
      </Section>
      <Section state={state} section="inflation" title="Inflation" source="FRED · BLS / BEA releases">
        {(b) => <IndicatorTable items={b.sections!.inflation!.items} yoy />}
      </Section>
      <Section state={state} section="jobs_growth" title="Jobs & growth" source="FRED · BLS / BEA releases">
        {(b) => <IndicatorTable items={b.sections!.jobs_growth!.items} />}
      </Section>
      <Section state={state} section="recession" title="Recession odds" source="Kalshi · market price">
        {(b) => { const r = b.sections!.recession!; return (
          <p className="big-figure"><b className="num">{oddsPct(r.prob)}</b> <span className="muted">{r.label}</span> <KindChip kind={r.kind} /></p>); }}
      </Section>
      <Section state={state} section="markets" title="Markets" source="Yahoo (unofficial, delayed) · Coinbase">
        {(b) => { const m = b.sections!.markets!; return (<>
          <table className="table"><tbody>{m.quotes.map((q) => (
            <tr key={q.symbol}><td>{q.label}</td><td className="num r">{q.price.toLocaleString(undefined, { maximumFractionDigits: 2 })}</td>
              <td className={`num r ${q.change_pct != null && q.change_pct < 0 ? "bad" : "good"}`}>
                {q.change_pct != null ? `${q.change_pct >= 0 ? "+" : ""}${q.change_pct.toFixed(2)}%` : ""}</td>
              <td className="muted small">{q.market_open ? "open" : "closed"}</td><td><KindChip kind={q.kind} /></td></tr>))}</tbody></table>
          {m.fallback.length > 0 && <p className="footnote">FRED daily close: {m.fallback.map((f) => `${f.label} ${f.value}`).join(" · ")}</p>}</>); }}
      </Section>
      <Section state={state} section="sentiment" title="Fear gauge" source="alternative.me · VIX in Markets">
        {(b) => { const s = b.sections!.sentiment!; return (
          <p className="big-figure"><b className="num">{s.value}</b> <span>{s.label}</span> <span className="muted">· {s.name}</span></p>); }}
      </Section>
    </>
  );
}
