export const pct = (p: number) => `${Math.round(p * 100)}%`;
export const cents = (p: number) => `${Math.round(p * 100)}¢`;
export const edge = (c: number) => `${c > 0 ? "+" : ""}${c.toFixed(1)}¢`;
