import { GameOfTheDay } from "../panels/GameOfTheDay";
import { MacroStrip } from "../panels/macro/MacroStrip";
import { JobsHealth } from "../panels/JobsHealth";
import { ModelVsMarket } from "../panels/ModelVsMarket";
import { NflSlate } from "../panels/NflSlate";

export function Home() {
  return (
    <div className="grid home-grid">
      <MacroStrip />
      <GameOfTheDay />
      <ModelVsMarket />
      <NflSlate />
      <JobsHealth />
    </div>
  );
}
