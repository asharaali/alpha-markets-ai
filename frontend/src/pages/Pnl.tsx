import { Panel } from "../components/Panel";
import { isDemo, useApi, type ApiState, type PanelStatus } from "../lib/api";

export type VenueName = "kalshi" | "public" | "topstep";
export interface Position { symbol: string; name: string | null; kind: string | null; qty: number | null;
  value: number | null; cost: number | null; pnl: number | null; day_pnl: number | null; note: string | null }
export interface Venue { venue: VenueName; ok: boolean; error: string | null; as_of: number | null; age_s: number | null;
  stale: boolean; equity: number | null; cash: number | null; day_pnl: number | null; unrealized_pnl: number | null;
  start_balance: number | null; note: string | null; positions: Position[] }
export interface PnlBlob { ready: boolean; received_at?: number; venues: Venue[];
  totals?: { equity: number | null; day_pnl: number | null; venues_counted: VenueName[]; venues_missing: VenueName[] };
  history: { ts: number; by_venue: Partial<Record<VenueName, number | null>> }[] }

const LABEL: Record<VenueName, string> = { kalshi: "Kalshi", public: "Public", topstep: "Topstep 50K Combine" };
const SOURCE: Record<VenueName, string> = {
  kalshi: "kalshi-monitor on your Mac · read-only",
  public: "Public API · read-only",
  topstep: "TopstepX / KRYVANT journal · read-only",
};

export const usd = (v: number | null | undefined, signed = false) => {
  if (v == null) return "—";
  const s = Math.abs(v).toLocaleString(undefined, { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return v < 0 ? `−${s}` : signed && v > 0 ? `+${s}` : s;
};
const tone = (v: number | null | undefined) => (v == null || v === 0 ? "" : v > 0 ? "good" : "bad");

/** Tiny equity line for one venue; gaps (failed pushes) are skipped, never drawn as zero. */
export function Spark({ points }: { points: number[] }) {
  if (points.length < 2) return <p className="footnote">Curve starts after a few pushes.</p>;
  const lo = Math.min(...points), hi = Math.max(...points), span = hi - lo || 1;
  const w = 300, h = 44;
  const d = points.map((p, i) => `${((i / (points.length - 1)) * w).toFixed(1)},${(h - ((p - lo) / span) * (h - 4) - 2).toFixed(1)}`).join(" ");
  const up = points[points.length - 1] >= points[0];
  return (
    <svg className="spark" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" role="img"
      aria-label={`equity from ${usd(points[0])} to ${usd(points[points.length - 1])}`}>
      <polyline points={d} fill="none" stroke={up ? "var(--good)" : "var(--bad)"} strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function VenuePanel({ v, page, history }: { v: Venue; page: ApiState<PnlBlob>; history: PnlBlob["history"] }) {
  const status: PanelStatus = page.status === "ERROR" ? "ERROR" : !v.ok ? "ERROR" : v.stale ? "STALE" : page.status;
  const curve = history.map((h) => h.by_venue[v.venue]).filter((x): x is number => x != null);
  const lifetime = v.start_balance != null && v.equity != null ? v.equity - v.start_balance : null;
  return (
    <Panel title={LABEL[v.venue]} source={SOURCE[v.venue]} status={status} asOf={v.as_of}
      error={v.error ?? page.error ?? "This venue did not report."}>
      <p className="big-figure"><b className="num">{usd(v.equity)}</b>
        {v.day_pnl != null && <span className={`num ${tone(v.day_pnl)}`}>{usd(v.day_pnl, true)} today</span>}</p>
      <dl className="kv">
        {v.cash != null && <><dt>Cash</dt><dd className="num">{usd(v.cash)}</dd></>}
        {v.unrealized_pnl != null && <><dt>Open P&amp;L</dt><dd className={`num ${tone(v.unrealized_pnl)}`}>{usd(v.unrealized_pnl, true)}</dd></>}
        {lifetime != null && <><dt>Since start</dt><dd className={`num ${tone(lifetime)}`}>{usd(lifetime, true)}</dd></>}
      </dl>
      <Spark points={curve} />
      {v.positions.length > 0 ? (
        <table className="table"><thead><tr><th>Position</th><th className="r">Qty</th><th className="r">Value</th>
          <th className="r">P&amp;L</th><th className="r">Today</th></tr></thead>
          <tbody>{v.positions.map((p) => (
            <tr key={`${p.symbol}-${p.kind}`} title={p.note ?? undefined}>
              <td>{p.name ?? p.symbol}<span className="muted small"> {p.kind?.toLowerCase()}</span></td>
              <td className="num r">{p.qty ?? "—"}</td><td className="num r">{usd(p.value)}</td>
              <td className={`num r ${tone(p.pnl)}`}>{usd(p.pnl, true)}</td>
              <td className={`num r ${tone(p.day_pnl)}`}>{usd(p.day_pnl, true)}</td></tr>))}</tbody></table>
      ) : <p className="muted">No open positions.</p>}
      {v.note && <p className="footnote">{v.note}</p>}
    </Panel>
  );
}

/** Demo never fetches P&L: a recruiter sees what the panel does, not any balance. */
export function Pnl() {
  return isDemo() ? <DemoPnl /> : <LivePnl />;
}

function DemoPnl() {
  return (
      <div className="grid pnl-grid">
        <Panel title="P&L" source="private" status="DEMO" asOf={null}>
          <p>The live terminal shows combined P&amp;L across Kalshi, Public and a Topstep futures account.
            Balances and positions are private, so they are not in the demo.</p>
          <p className="footnote">How it works: a job on the owner's Mac reads each broker read-only and pushes a
            snapshot with a token. Broker keys never touch the server.</p>
        </Panel>
      </div>
  );
}

function LivePnl() {
  const state = useApi<PnlBlob>("/pnl", { maxAgeSec: 1800, asOf: (d) => d.received_at ?? null, refreshSec: 120 });
  const { data } = state;
  if (state.status === "ERROR" && !data) {
    return <div className="grid pnl-grid"><Panel title="P&L" source="/api/pnl" status="ERROR" asOf={null} error={state.error} /></div>;
  }
  if (!data || !data.ready) {
    return (
      <div className="grid pnl-grid">
        <Panel title="P&L" source="Mac push job" status={data ? "STALE" : "LOADING"} asOf={null}>
          <p>No snapshot yet. The Mac job (<code>mac/pnl_push.py</code>) has not pushed one to this server.</p>
        </Panel>
      </div>
    );
  }
  const t = data.totals!;
  return (
    <div className="grid pnl-grid">
      <Panel title="All accounts" source="sum of venues that reported" status={state.status} asOf={state.asOf}
        className="pnl-total">
        <p className="big-figure"><b className="num">{usd(t.equity)}</b>
          {t.day_pnl != null && <span className={`num ${tone(t.day_pnl)}`}>{usd(t.day_pnl, true)} today</span>}</p>
        <p className="footnote">Counted: {t.venues_counted.map((v) => LABEL[v]).join(", ") || "none"}
          {t.venues_missing.length > 0 && <span className="bad"> · missing: {t.venues_missing.map((v) => LABEL[v]).join(", ")}</span>}
          {" "}· Today = venues that report a day figure.</p>
      </Panel>
      {data.venues.map((v) => <VenuePanel key={v.venue} v={v} page={state} history={data.history} />)}
    </div>
  );
}
