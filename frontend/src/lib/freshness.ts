/** LIVE only while the data is provably younger than maxAgeSec; unknown age is STALE. */
export function freshness(asOf: number | null, maxAgeSec: number, now = Date.now() / 1000): "LIVE" | "STALE" {
  if (asOf == null || !Number.isFinite(asOf)) return "STALE";
  return now - asOf <= maxAgeSec ? "LIVE" : "STALE";
}
