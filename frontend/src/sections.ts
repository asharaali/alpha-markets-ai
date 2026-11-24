/** Every top-level section, and the spec module that builds it (null = built). */
export const SECTIONS = [
  { path: "", label: "Home", module: null, title: "Home" },
  { path: "board", label: "Board", module: null, title: "Trade Board" },
  { path: "markets", label: "Markets", module: null, title: "Prediction-market terminal" },
  { path: "sports", label: "Sports", module: 5, title: "Sports" },
  { path: "macro", label: "Macro", module: null, title: "Macro & rates" },
  { path: "politics", label: "Politics", module: 6, title: "Politics" },
  { path: "pnl", label: "P&L", module: null, title: "P&L" },
  { path: "research", label: "Research", module: null, title: "Research & analysis" },
] as const;

export type Section = (typeof SECTIONS)[number];
