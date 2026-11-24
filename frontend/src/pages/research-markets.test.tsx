import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { stubFetch } from "../test-fetch";
import { Markets } from "./Markets";
import { Research } from "./Research";

afterEach(() => vi.unstubAllGlobals());

const EVIDENCE = {
  thresholds: { min_calls: 150, min_clusters: 40 },
  families: [
    { strategy: "book_consensus", market_type: "total", state: "FAILING", calls: 92, clusters: 162,
      model_log_loss: 0.457, market_log_loss: 0.435, difference: 0.02, ci95: [0.007, 0.056], roi_after_fees: -0.08,
      reason: "Significantly worse than the market price.",
      calibration: [{ bucket: "10-20%", n: 167, predicted: 0.146, observed: 0.06 }] },
  ],
};
const JOURNAL = { entries: [], summary: {} };
const BRIEFS_OFF = { enabled: false, reason: "No ANTHROPIC_API_KEY configured." };

describe("Research page", () => {
  it("shows each family's verdict, the reason, the thresholds and its calibration", async () => {
    stubFetch({ "/api/research/evidence": [200, EVIDENCE], "/api/journal": [200, JOURNAL], "/api/research/briefs": [200, BRIEFS_OFF] });
    render(<MemoryRouter><Research /></MemoryRouter>);
    const panel = await screen.findByRole("region", { name: "Evidence gate" });
    const row = (await within(panel).findByText("book_consensus · total")).closest("tr")!;
    expect(within(row).getByText("FAILING")).toBeInTheDocument();
    expect(within(row).getByText("Significantly worse than the market price.")).toBeInTheDocument();
    expect(screen.getByText("PROVEN needs ≥150 calls, ≥40 games, interval below 0, ROI after fees > 0.")).toBeInTheDocument();
    const cal = screen.getByRole("region", { name: "Calibration" });
    expect(within(cal).getByText("15%")).toBeInTheDocument();   // predicted
    expect(within(cal).getByText("6%")).toBeInTheDocument();    // observed
  });

  it("says briefs are off when no API key is configured", async () => {
    stubFetch({ "/api/research/evidence": [200, EVIDENCE], "/api/journal": [200, JOURNAL], "/api/research/briefs": [200, BRIEFS_OFF] });
    render(<MemoryRouter><Research /></MemoryRouter>);
    expect(await screen.findByText("Briefs are off: No ANTHROPIC_API_KEY configured.")).toBeInTheDocument();
  });
});

const BROWSE = {
  as_of: Date.now() / 1000, categories: ["Economics", "Sports"],
  warnings: ["Polymarket US unavailable: gateway timeout"],
  markets: [
    { venue: "kalshi", id: "K1", title: "Fed holds in December?", category: "Economics", yes_bid: 0.86, yes_ask: 0.88, last: 0.87, volume: 50000, closes: "2026-12-16T19:00:00Z", url: "https://kalshi.com/markets/kxfed", mid: 0.87 },
    { venue: "kalshi", id: "K2", title: "Chiefs win Super Bowl?", category: "Sports", yes_bid: 0.14, yes_ask: 0.16, last: 0.15, volume: 9000, closes: null, url: "https://kalshi.com/markets/kxsb", mid: 0.15 },
  ],
};

describe("Markets page", () => {
  it("lists venues side by side, keeps one venue's warning visible, and search filters", async () => {
    stubFetch({ "/api/markets/browse": [200, BROWSE] });
    render(<MemoryRouter><Markets /></MemoryRouter>);
    expect(await screen.findByText("Fed holds in December?")).toBeInTheDocument();
    expect(screen.getByText("Polymarket US unavailable: gateway timeout")).toBeInTheDocument();
    await userEvent.type(screen.getByRole("searchbox", { name: "Filter markets" }), "chiefs");
    expect(screen.queryByText("Fed holds in December?")).not.toBeInTheDocument();
    expect(screen.getByText("Chiefs win Super Bowl?")).toBeInTheDocument();
  });
});
