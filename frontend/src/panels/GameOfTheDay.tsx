import { Panel } from "../components/Panel";
import { useApi } from "../lib/api";
import { kickoffTime, pct, slateAsOf, type Slate, type SlateEntry } from "../lib/slate";

/** The unplayed game the model thinks is closest: smallest |expected margin|. */
export function pickGameOfTheDay(slate: Slate): SlateEntry | undefined {
  return (slate.games as SlateEntry[])
    .filter((g) => !g.game.completed && g.projection)
    .sort((a, b) => Math.abs(a.projection!.expected_margin) - Math.abs(b.projection!.expected_margin))[0];
}

export function GameOfTheDay() {
  const { data, status, asOf, error } = useApi<Slate>("/slate", { maxAgeSec: 3600, asOf: slateAsOf, refreshSec: 300 });
  const pick = data && pickGameOfTheDay(data);
  const p = pick?.projection;
  return (
    <Panel title="Game of the day" source="Alpha Markets model" status={status} asOf={asOf} error={error} className="gotd">
      {!pick || !p ? <p className="muted">No unplayed games on this week's slate.</p> : (
        <div className="matchup">
          <div className="side"><span className="abbr">{p.away}</span><span className="num big">{pct(p.win_probability.away)}</span>
            {p.projected_score && <span className="num muted">{p.projected_score.away.toFixed(0)}</span>}</div>
          <div className="at"><span>@</span><time>{kickoffTime(p.kickoff)}</time></div>
          <div className="side"><span className="abbr">{p.home}</span><span className="num big">{pct(p.win_probability.home)}</span>
            {p.projected_score && <span className="num muted">{p.projected_score.home.toFixed(0)}</span>}</div>
          <p className="footnote">Closest projected game on the slate · model margin {Math.abs(p.expected_margin).toFixed(1)} pts</p>
        </div>
      )}
    </Panel>
  );
}
