import { useEffect, useState } from "react";
import { freshness } from "./freshness";

export type PanelStatus = "LIVE" | "LOADING" | "STALE" | "ERROR" | "DEMO";

export class UnauthorizedError extends Error {
  constructor() { super("unauthorized"); this.name = "UnauthorizedError"; }
}

/** Fired on any 401 so the app can drop to the login screen in one place. */
export const UNAUTHORIZED_EVENT = "am:unauthorized";

export const isDemo = (): boolean => window.location.pathname.startsWith("/demo");

/** `/status/jobs` → `/static/terminal/demo/status_jobs.json` */
export const demoUrl = (path: string): string =>
  `/static/terminal/demo/${path.replace(/^\//, "").replace(/\//g, "_")}.json`;

export async function apiGet<T>(path: string): Promise<T> {
  if (isDemo()) {
    const res = await fetch(demoUrl(path));
    if (!res.ok) throw new Error(`demo snapshot missing for ${path}`);
    return res.json() as Promise<T>;
  }
  const res = await fetch(`/api${path}`, { credentials: "include" });
  if (res.status === 401) {
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    throw new UnauthorizedError();
  }
  if (!res.ok) {
    let message = `${res.status}`;
    try { message = (await res.json()).message ?? message; } catch { /* non-JSON body */ }
    throw new Error(message);
  }
  return res.json() as Promise<T>;
}

export interface ApiState<T> { data?: T; status: PanelStatus; asOf: number | null; error?: string }

export function useApi<T>(path: string, opts: {
  maxAgeSec: number; asOf: (d: T) => number | null; refreshSec?: number;
}): ApiState<T> {
  const { maxAgeSec, refreshSec } = opts;
  const [state, setState] = useState<ApiState<T>>({ status: "LOADING", asOf: null });
  const asOfRef = opts.asOf;
  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const data = await apiGet<T>(path);
        if (!alive) return;
        const asOf = asOfRef(data);
        setState({ data, asOf, status: isDemo() ? "DEMO" : freshness(asOf, maxAgeSec) });
      } catch (e) {
        if (alive) setState((s) => ({ ...s, status: "ERROR", error: (e as Error).message }));
      }
    };
    load();
    const timer = refreshSec && !isDemo() ? window.setInterval(load, refreshSec * 1000) : undefined;
    return () => { alive = false; if (timer) window.clearInterval(timer); };
    // asOf extractor is expected to be stable per call site
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, maxAgeSec, refreshSec]);
  return state;
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  if (isDemo()) throw new Error("Trading and logging are disabled in the demo.");
  const res = await fetch(`/api${path}`, {
    method: "POST", credentials: "include",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (res.status === 401) { window.dispatchEvent(new Event(UNAUTHORIZED_EVENT)); throw new UnauthorizedError(); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message ?? `${res.status}`);
  return data as T;
}
