import { useMemo, useState } from "react";
import { Panel } from "../components/Panel";
import { useApi } from "../lib/api";

interface Market { venue: "kalshi" | "polymarket"; id: string; title: string; category: string; yes_bid: number | null;
  yes_ask: number | null; last: number | null; volume: number | null; closes: string | null; url: string; mid: number | null }
interface Browse { markets: Market[]; categories: string[]; warnings: string[]; as_of: number }

const c = (p: number | null) => (p == null ? "—" : `${Math.round(p * 100)}¢`);
const VENUES = [{ key: "kalshi", name: "Kalshi" }, { key: "polymarket", name: "Polymarket US" }] as const;

export function Markets() {
  const { data, status, asOf, error } = useApi<Browse>("/markets/browse", { maxAgeSec: 600, asOf: (d) => d.as_of, refreshSec: 120 });
  const [q, setQ] = useState("");
  const [cat, setCat] = useState("");
  const rows = useMemo(() => (data?.markets ?? []).filter((m) =>
    (!q || m.title.toLowerCase().includes(q.toLowerCase())) && (!cat || m.category === cat)), [data, q, cat]);
  return (
    <div className="markets-page">
      <div className="markets-controls">
        <input type="search" aria-label="Filter markets" placeholder="Filter markets…" value={q} onChange={(e) => setQ(e.target.value)} />
        <select aria-label="Category" value={cat} onChange={(e) => setCat(e.target.value)}>
          <option value="">All categories</option>{(data?.categories ?? []).map((x) => <option key={x}>{x}</option>)}
        </select>
        {(data?.warnings ?? []).map((w) => <span key={w} className="bad">{w}</span>)}
      </div>
      <div className="grid two-col">
        {VENUES.map((v) => {
          const list = rows.filter((m) => m.venue === v.key).slice(0, 150);
          return (
            <Panel key={v.key} title={v.name} source={v.key === "kalshi" ? "Kalshi public API" : "Polymarket US gateway · read-only"} status={status} asOf={asOf} error={error}>
              <table className="table"><thead><tr><th>Market</th><th className="r">Bid</th><th className="r">Ask</th><th className="r">Closes</th></tr></thead>
                <tbody>{list.map((m) => (
                  <tr key={m.id}><td><a href={m.url} target="_blank" rel="noopener">{m.title}</a><div className="muted small">{m.category}</div></td>
                    <td className="num r">{c(m.yes_bid)}</td><td className="num r">{c(m.yes_ask ?? m.last)}</td>
                    <td className="num r">{m.closes ? new Date(m.closes).toLocaleDateString([], { month: "short", day: "numeric" }) : "—"}</td></tr>
                ))}</tbody></table>
              {list.length === 0 && <p className="muted">Nothing here{q || cat ? " matches" : ""}.</p>}
            </Panel>
          );
        })}
      </div>
    </div>
  );
}
