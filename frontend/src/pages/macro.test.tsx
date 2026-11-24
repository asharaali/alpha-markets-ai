import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import live from "../panels/macro/fixture.json";
import { MacroStrip } from "../panels/macro/MacroStrip";
import { stubFetch } from "../test-fetch";
import { Macro } from "./Macro";

afterEach(() => vi.unstubAllGlobals());
const fresh = () => ({ ...live, built_at: Date.now() / 1000 });
const page = () => render(<MemoryRouter><Macro /></MemoryRouter>);

describe("Macro page", () => {
  it("labels what each number is: a print, a market price, or a quote", async () => {
    stubFetch({ "/api/macro": [200, fresh()] });
    page();
    const rates = await screen.findByRole("region", { name: "Treasury yields" });
    expect(within(rates).getByText("10-Year")).toBeInTheDocument();
    expect(within(rates).getByText("4.92%")).toBeInTheDocument();
    expect(within(rates).getAllByText("PRINT").length).toBeGreaterThan(0);
    const fed = screen.getByRole("region", { name: "Fed decision odds" });
    expect(within(fed).getByText("Hike 25bp")).toBeInTheDocument();
    expect(within(fed).getByText("92%")).toBeInTheDocument();
    expect(within(fed).getByText("MARKET")).toBeInTheDocument();
    const mk = screen.getByRole("region", { name: "Markets" });
    expect(within(mk).getAllByText("QUOTE").length).toBeGreaterThan(0);
    expect(within(screen.getByRole("region", { name: "Inflation" })).getByText("4.08%")).toBeInTheDocument();
  });

  it("a failed section errors in its own panel while the rest render", async () => {
    const blob = fresh() as typeof live & { errors: Record<string, string> };
    const { recession: _drop, ...rest } = blob.sections;
    stubFetch({ "/api/macro": [200, { ...blob, sections: rest, errors: { recession: "RuntimeError: no priced Kalshi recession market" } }] });
    page();
    const rec = await screen.findByRole("region", { name: "Recession odds" });
    expect(within(rec).getByText("RuntimeError: no priced Kalshi recession market")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Treasury yields" })).getByText("4.92%")).toBeInTheDocument();
  });

  it("shows LOADING, not an error, before the first refresh has run", async () => {
    stubFetch({ "/api/macro": [200, { ready: false }] });
    page();
    const rates = await screen.findByRole("region", { name: "Treasury yields" });
    expect(await within(rates).findByText("LOADING")).toBeInTheDocument();
  });
});

describe("Home macro strip", () => {
  it("shows the 10Y, the most likely Fed outcome, CPI YoY and recession odds", async () => {
    stubFetch({ "/api/macro": [200, fresh()] });
    render(<MemoryRouter><MacroStrip /></MemoryRouter>);
    expect(await screen.findByText("4.92%")).toBeInTheDocument();
    expect(screen.getByText("Hike 25bp 92%")).toBeInTheDocument();
    expect(screen.getByText("4.08%")).toBeInTheDocument();
    expect(screen.getByText("2.5%")).toBeInTheDocument();
  });
});
