import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { stubFetch } from "../test-fetch";
import { Pnl, type PnlBlob } from "./Pnl";

afterEach(() => { vi.unstubAllGlobals(); window.history.pushState({}, "", "/"); });
const page = () => render(<MemoryRouter><Pnl /></MemoryRouter>);
const now = () => Date.now() / 1000;

const blob = (): PnlBlob => ({
  ready: true, received_at: now(),
  totals: { equity: 51500.5, day_pnl: -7.5, venues_counted: ["kalshi", "public", "topstep"], venues_missing: [] },
  venues: [
    { venue: "kalshi", ok: true, error: null, as_of: now(), age_s: 10, stale: false, equity: 100.5, cash: 100.5,
      day_pnl: null, unrealized_pnl: -4.0, start_balance: null, note: "Valued at executable bid.",
      positions: [{ symbol: "KXEXAMPLE-26-T1", name: "Example market", kind: "single:yes", qty: 10,
        value: 0, cost: 4.0, pnl: -4.0, day_pnl: null, note: "unsellable part valued $0" }] },
    { venue: "public", ok: true, error: null, as_of: now(), age_s: 10, stale: false, equity: 1000, cash: 250,
      day_pnl: -7.5, unrealized_pnl: -40, start_balance: null, note: null,
      positions: [{ symbol: "ACME", name: "Acme Corp", kind: "EQUITY", qty: 3, value: 300, cost: 330, pnl: -30, day_pnl: -6, note: null }] },
    { venue: "topstep", ok: true, error: null, as_of: now() - 5 * 3600, age_s: 5 * 3600, stale: false, equity: 50400,
      cash: null, day_pnl: null, unrealized_pnl: null, start_balance: 50000, note: "Runner is live.", positions: [] },
  ],
  history: [{ ts: 1, by_venue: { public: 1020, kalshi: 101, topstep: 50300 } },
            { ts: 2, by_venue: { public: null, kalshi: 101, topstep: 50350 } },
            { ts: 3, by_venue: { public: 1000, kalshi: 100.5, topstep: 50400 } }],
});

describe("P&L page", () => {
  it("shows the combined total, each venue, and positions", async () => {
    stubFetch({ "/api/pnl": [200, blob()] });
    page();
    const total = await screen.findByRole("region", { name: "All accounts" });
    expect(within(total).getByText("$51,500.50")).toBeInTheDocument();
    expect(within(total).getByText("−$7.50 today")).toBeInTheDocument();
    const pub = screen.getByRole("region", { name: "Public" });
    expect(within(pub).getByText("Acme Corp")).toBeInTheDocument();
    expect(within(pub).getByText("−$30.00")).toBeInTheDocument();
    const ts = screen.getByRole("region", { name: "Topstep 50K Combine" });
    expect(within(ts).getByText("+$400.00")).toBeInTheDocument();      // since the $50k start
    expect(within(ts).getByText("No open positions.")).toBeInTheDocument();
  });

  it("a failed venue errors in its own panel and is named as missing from the total", async () => {
    const b = blob();
    b.venues[2] = { ...b.venues[2], ok: false, stale: true, error: "TopstepX login failed (errorCode=3)", equity: null };
    b.totals = { ...b.totals!, equity: 1100.5, venues_counted: ["kalshi", "public"], venues_missing: ["topstep"] };
    stubFetch({ "/api/pnl": [200, b] });
    page();
    const ts = await screen.findByRole("region", { name: "Topstep 50K Combine" });
    expect(within(ts).getByText("ERROR")).toBeInTheDocument();
    expect(within(ts).getByText("TopstepX login failed (errorCode=3)")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "All accounts" })).getByText(/missing: Topstep/)).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Public" })).getByText("$1,000.00")).toBeInTheDocument();
  });

  it("marks a stale venue STALE", async () => {
    const b = blob();
    b.venues[1] = { ...b.venues[1], stale: true };
    stubFetch({ "/api/pnl": [200, b] });
    page();
    const pub = await screen.findByRole("region", { name: "Public" });
    expect(within(pub).getByText("STALE")).toBeInTheDocument();
  });

  it("the equity curve skips failed pushes instead of drawing a zero", async () => {
    stubFetch({ "/api/pnl": [200, blob()] });
    page();
    const pub = await screen.findByRole("region", { name: "Public" });
    const svg = within(pub).getByRole("img");
    expect(svg.getAttribute("aria-label")).toBe("equity from $1,020.00 to $1,000.00");
    expect(svg.querySelector("polyline")!.getAttribute("points")!.split(" ")).toHaveLength(2);
  });

  it("before the first push it explains, rather than erroring", async () => {
    stubFetch({ "/api/pnl": [200, { ready: false, venues: [], history: [] }] });
    page();
    expect(await screen.findByText(/has not pushed one/)).toBeInTheDocument();
  });

  it("the demo never requests P&L and shows no balances", async () => {
    window.history.pushState({}, "", "/demo/pnl");
    const { calls } = stubFetch({});
    page();
    expect(await screen.findByText(/Balances and positions are private/)).toBeInTheDocument();
    expect(calls).toEqual([]);
  });
});
