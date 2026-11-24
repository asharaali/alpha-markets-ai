import { useApi } from "../lib/api";
import { MacroPanels } from "../panels/macro/panels";
import type { MacroBlob } from "../panels/macro/types";

export function Macro() {
  const state = useApi<MacroBlob>("/macro", { maxAgeSec: 1800, asOf: (d) => d.built_at ?? null, refreshSec: 300 });
  return <div className="grid macro-grid"><MacroPanels state={state} /></div>;
}
