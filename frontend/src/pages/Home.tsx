import { GameOfTheDay } from "../panels/GameOfTheDay";
import { JobsHealth } from "../panels/JobsHealth";
import { ModelVsMarket } from "../panels/ModelVsMarket";
import { NflSlate } from "../panels/NflSlate";

export function Home() {
  return (
    <div className="grid home-grid">
      <GameOfTheDay />
      <ModelVsMarket />
      <NflSlate />
      <JobsHealth />
    </div>
  );
}
