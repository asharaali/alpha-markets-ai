import { Link } from "react-router";
import { Panel } from "../../components/Panel";
import { isDemo, useApi } from "../../lib/api";
import { oddsPct, pct0, rate, type MacroBlob } from "./types";

export function MacroStrip() {
  const state = useApi<MacroBlob>("/macro", { maxAgeSec: 1800, asOf: (d) => d.built_at ?? null, refreshSec: 300 });
  const s = state.data?.sections;
  const ten = s?.rates?.items.find((i) => i.id === "DGS10");
  const cpi = s?.inflation?.items.find((i) => i.id === "CPIAUCSL");
  const top = s?.fed?.outcomes[0];
  const spy = s?.markets?.quotes.find((q) => q.symbol === "SPY");
  const status = state.data && !state.data.ready ? "LOADING" : state.status;
  const tiles: [string, string | undefined][] = [
    ["10-Year", ten && rate(ten.value)], ["Fed next", top && `${top.label} ${pct0(top.prob)}`],
    ["CPI YoY", cpi?.yoy != null ? rate(cpi.yoy) : undefined], ["Recession", s?.recession && oddsPct(s.recession.prob)],
    ["SPY", spy && `${spy.price.toFixed(2)}`],
  ];
  return (
    <Panel title="Macro" source="FRED · Kalshi · Yahoo" status={status} asOf={state.asOf} error={state.error} className="wide"
      actions={<Link className="mini-link" to={`${isDemo() ? "/demo" : ""}/macro`}>All macro →</Link>}>
      <div className="strip">{tiles.map(([k, v]) => (
        <div key={k} className="tile"><span className="muted small">{k}</span><b className="num">{v ?? "—"}</b></div>))}</div>
    </Panel>
  );
}
