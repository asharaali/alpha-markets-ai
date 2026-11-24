import { Panel } from "../components/Panel";
import { useApi } from "../lib/api";

interface Briefs { enabled: boolean; source?: "template" | "ai"; reason?: string; error?: string; generated_at?: number; briefs?: { card_id: string; title?: string; text: string }[] }

export function BriefsPanel() {
  const { data, status, asOf, error } = useApi<Briefs>("/research/briefs", { maxAgeSec: 6 * 3600, asOf: (d) => d.generated_at ?? null, refreshSec: 1800 });
  return (
    <Panel title="Research briefs" source={data?.source === "ai" ? "Claude · from each card's own data, cited" : "written from each card's own data"}
      status={status} asOf={asOf} error={error} className="wide">
      {data && !data.enabled && <p className="muted">Briefs are off: {data.reason}</p>}
      {data?.error && <p className="bad">{data.error}</p>}
      {data?.enabled && (data.briefs ?? []).length === 0 && !data.error && <p className="muted">No board cards to brief right now.</p>}
      {(data?.briefs ?? []).map((b) => (
        <article key={b.card_id} className="brief">{b.title && <h3>{b.title}</h3>}<p>{b.text}</p></article>
      ))}
    </Panel>
  );
}
