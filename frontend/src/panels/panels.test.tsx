import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { stubFetch } from "../test-fetch";
import { SLATE_FULL, STRATEGIES, jobs } from "./fixtures";
import { GameOfTheDay, pickGameOfTheDay } from "./GameOfTheDay";
import { JobsHealth } from "./JobsHealth";
import { ModelVsMarket } from "./ModelVsMarket";
import { NflSlate } from "./NflSlate";

afterEach(() => vi.unstubAllGlobals());

describe("ModelVsMarket", () => {
  it("marks a strategy whose log loss is above the market's as WORSE THAN MARKET", async () => {
    stubFetch({ "/api/strategies": [200, STRATEGIES] });
    render(<ModelVsMarket />);
    const row = (await screen.findByText("Moneyline")).closest("tr")!;
    expect(within(row).getByText("WORSE THAN MARKET")).toBeInTheDocument();
    expect(within(row).getByText("0.319")).toBeInTheDocument();
    expect(within(row).getByText("0.287")).toBeInTheDocument();
  });

  it("leaves out strategies with no meaningful record", async () => {
    stubFetch({ "/api/strategies": [200, STRATEGIES] });
    render(<ModelVsMarket />);
    await screen.findByText("Moneyline");
    expect(screen.queryByText("Situational")).not.toBeInTheDocument();
  });

  it("never shows BEATS MARKET when no strategy beats it", async () => {
    stubFetch({ "/api/strategies": [200, STRATEGIES] });
    render(<ModelVsMarket />);
    await screen.findByText("Moneyline");
    expect(screen.queryByText("BEATS MARKET")).not.toBeInTheDocument();
  });
});

describe("GameOfTheDay", () => {
  it("picks the most competitive unplayed game and skips finished ones", () => {
    expect(pickGameOfTheDay(SLATE_FULL)?.game.game_id).toBe("2026_11_CIN_WAS");
  });

  it("renders teams and the model win %", async () => {
    stubFetch({ "/api/slate": [200, SLATE_FULL] });
    render(<GameOfTheDay />);
    expect(await screen.findByText("CIN")).toBeInTheDocument();
    expect(screen.getByText("WAS")).toBeInTheDocument();
    expect(screen.getByText("60%")).toBeInTheDocument();  // CIN = 1 - 0.40 - 0.004 → 60%
  });
});

describe("NflSlate", () => {
  it("lists every game with a final score or a kickoff", async () => {
    stubFetch({ "/api/slate": [200, SLATE_FULL] });
    render(<NflSlate />);
    expect(await screen.findByText(/26/)).toBeInTheDocument();
    expect(screen.getAllByRole("row")).toHaveLength(3 + 1);
  });
});

describe("JobsHealth", () => {
  it("flags a 30-minute job that last ran 4 hours ago as STALE", async () => {
    const now = Date.now() / 1000;
    stubFetch({ "/api/status/jobs": [200, jobs(now)] });
    render(<JobsHealth />);
    const grading = (await screen.findByText("grading")).closest("tr")!;
    expect(within(grading).getByText("STALE")).toBeInTheDocument();
    const snaps = screen.getByText("market_snapshots").closest("tr")!;
    expect(within(snaps).getByText("OK")).toBeInTheDocument();
  });
});

describe("JobsHealth in demo mode", () => {
  afterEach(() => window.history.pushState({}, "", "/"));
  it("measures job age against the snapshot, so an old demo never reads STALE", async () => {
    window.history.pushState({}, "", "/demo");
    const snapshotTime = Date.now() / 1000 - 30 * 86400;   // snapshot taken a month ago
    const old = { enabled: true, running: true, jobs: {
      grading: { runs: 1, errors: 0, last_run: snapshotTime - 300, last_error: null },
      market_snapshots: { runs: 1, errors: 0, last_run: snapshotTime, last_error: null } } };
    stubFetch({ "/static/terminal/demo/status_jobs.json": [200, old] });
    render(<JobsHealth />);
    const grading = (await screen.findByText("grading")).closest("tr")!;
    expect(within(grading).getByText("OK")).toBeInTheDocument();
    expect(within(grading).getByText("5m")).toBeInTheDocument();
  });
});
