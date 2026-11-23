import { useMemo, useState, type FormEvent } from "react";
import { useNavigate } from "react-router";
import { useApi } from "../lib/api";
import { slateAsOf, type Slate } from "../lib/slate";
import { SECTIONS } from "../sections";

interface Hit { label: string; to: string }

export function Search({ base }: { base: string }) {
  const navigate = useNavigate();
  const [q, setQ] = useState("");
  const { data } = useApi<Slate>("/slate", { maxAgeSec: 3600, asOf: slateAsOf });
  const index = useMemo<Hit[]>(() => {
    const teams = new Set(data?.games.flatMap((g) => [g.game.home, g.game.away]) ?? []);
    return [
      ...SECTIONS.map((s) => ({ label: s.label, to: `${base}/${s.path}` })),
      ...[...teams].map((t) => ({ label: t, to: `${base}/board?q=${t}` })),
    ];
  }, [data, base]);
  const hits = q ? index.filter((h) => h.label.toLowerCase().includes(q.toLowerCase())).slice(0, 6) : [];
  const go = (e: FormEvent) => { e.preventDefault(); if (hits[0]) { navigate(hits[0].to); setQ(""); } };
  return (
    <form className="search" onSubmit={go} role="search">
      <input aria-label="Search" placeholder="Search teams, sections…" value={q} onChange={(e) => setQ(e.target.value)} />
      {hits.length > 0 && (
        <ul className="search-hits">{hits.map((h) => (
          <li key={h.to}><button type="button" onClick={() => { navigate(h.to); setQ(""); }}>{h.label}</button></li>
        ))}</ul>
      )}
    </form>
  );
}
