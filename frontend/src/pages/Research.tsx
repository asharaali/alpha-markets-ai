import { useApi } from "../lib/api";
import { BriefsPanel } from "../panels/Briefs";
import { Calibration } from "../panels/Calibration";
import { EvidenceTable } from "../panels/EvidenceTable";
import { Journal } from "../panels/Journal";
import type { EvidenceResponse } from "../research/types";

export function Research() {
  const evidence = useApi<EvidenceResponse>("/research/evidence", { maxAgeSec: 3600, asOf: () => Date.now() / 1000, refreshSec: 1800 });
  return (
    <div className="grid research-grid">
      <EvidenceTable state={evidence} />
      <Calibration state={evidence} />
      <Journal />
      <BriefsPanel />
    </div>
  );
}
