import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import fx from "../panels/sports/fixture.json";
import { stubFetch } from "../test-fetch";
import { Sports, type SportsIndex } from "./Sports";

afterEach(() => { vi.unstubAllGlobals(); window.history.pushState({}, "", "/"); });
const now = () => Date.now() / 1000;
const index = (): SportsIndex => ({ ...fx.index, built_at: now() });
const nba = () => ({ ...fx.nba, built_at: now() });
const page = (url = "/sports") => render(<MemoryRouter initialEntries={[url]}><Sports /></MemoryRouter>);

describe("Sports page", () => {
  it("opens the first league with live games and shows Elo beside the book line", async () => {
    stubFetch({ "/api/sports": [200, index()], "/api/sports/nba": [200, nba()] });
    page();
    await screen.findByText("Spurs");
    const slate = screen.getByRole("region", { name: "Scores & slate" });
    expect(screen.getByRole("button", { name: /NBA/, pressed: true })).toBeInTheDocument();
    expect(within(slate).getAllByText("LIVE", { selector: ".sp-live" })).toHaveLength(2);
    expect(within(slate).queryByText("Heat")).toBeNull();            // tomorrow sits behind the button
    fireEvent.click(within(slate).getByRole("button", { name: /Next few days/ }));
    const row = within(slate).getByText("Heat").closest("tr")!;
    expect(within(row).getByText("62%")).toBeInTheDocument();       // Elo home
    expect(within(row).getByText("52%")).toBeInTheDocument();       // book, vig removed
    expect(within(row).getByText("+10")).toHaveClass("warn");       // big gap flagged, not sold as edge
    expect(within(slate).getByText(/unproven, not a recommendation/)).toBeInTheDocument();
  });

  it("ratings, game of the day and an honest track record", async () => {
    stubFetch({ "/api/sports": [200, index()], "/api/sports/nba": [200, nba()] });
    page();
    await screen.findByText("Spurs");
    const ratings = screen.getByRole("region", { name: "Power ratings" });
    expect(within(ratings).getAllByRole("row")[1]).toHaveTextContent("Spurs");
    expect(within(ratings).getByRole("button", { name: "Show all 30" })).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Game of the day" })).getByText(/@/)).toBeInTheDocument();
    const rec = screen.getByRole("region", { name: "Elo track record" });
    expect(within(rec).getByText("66%")).toBeInTheDocument();
    expect(within(rec).getByText("WATCHING")).toBeInTheDocument();
    expect(within(rec).getByText(/No pregame lines captured yet/)).toBeInTheDocument();
  });

  it("switching league requests only that league", async () => {
    const { calls } = stubFetch({ "/api/sports": [200, index()], "/api/sports/nba": [200, nba()],
      "/api/sports/nhl": [200, { ...nba(), key: "nhl", label: "NHL" }] });
    page();
    await screen.findByText("Spurs");
    fireEvent.click(screen.getByRole("button", { name: /NHL/ }));
    await screen.findByRole("button", { name: /NHL/, pressed: true });
    expect(calls).toContain("/api/sports/nhl");
    expect(calls.filter((c) => c === "/api/sports/ncaab")).toEqual([]);
  });

  it("a league whose refresh failed is flagged on its tab", async () => {
    const idx = index();
    idx.leagues = idx.leagues.map((l) => (l.key === "nhl" ? { ...l, errors: ["RuntimeError: ESPN 503"] } : l));
    stubFetch({ "/api/sports": [200, idx], "/api/sports/nba": [200, nba()] });
    page();
    const tab = await screen.findByRole("button", { name: /NHL/ });
    expect(within(tab).getByTitle("RuntimeError: ESPN 503")).toBeInTheDocument();
  });

  it("before the first build it explains instead of erroring", async () => {
    stubFetch({ "/api/sports": [200, { ready: false, leagues: [] }] });
    page();
    expect(await screen.findByText(/No sports data yet/)).toBeInTheDocument();
  });

  it("a league named in the URL wins over the default", async () => {
    const { calls } = stubFetch({ "/api/sports": [200, index()], "/api/sports/mlb": [200, { ...nba(), key: "mlb", label: "MLB",
      status: "offseason", slate: [] }] });
    page("/sports?league=mlb");
    expect(await screen.findByText(/Offseason: no games scheduled/)).toBeInTheDocument();
    expect(calls).not.toContain("/api/sports/nba");
  });
});
