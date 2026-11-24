import type { EvidenceState } from "../board/types";

export interface CalRow { bucket: string; n: number; predicted: number; observed: number }
export interface Family {
  strategy: string; market_type: string; state: EvidenceState; calls: number; clusters: number;
  model_log_loss: number | null; market_log_loss: number | null; difference: number | null;
  ci95: [number, number] | null; roi_after_fees: number | null; reason: string; calibration: CalRow[];
}
export interface EvidenceResponse { thresholds: { min_calls: number; min_clusters: number }; families: Family[] }
export const famName = (f: Family) => `${f.strategy} · ${f.market_type}`;
