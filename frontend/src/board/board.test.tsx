import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Board } from "../pages/Board";
import { stubFetch } from "../test-fetch";
import { board, card, watching } from "./fixtures";

afterEach(() => vi.unstubAllGlobals());

const renderBoard = (path = "/board") =>
  render(<MemoryRouter initialEntries={[path]}><Board /></MemoryRouter>);

describe("Trade Board", () => {
  it("shows only proven trades by default and says how many are being watched", async () => {
    stubFetch({ "/api/board": [200, board([watching("w1", "Jets at Bills"), watching("w2", "Rams at Seahawks")])] });
    renderBoard();
    expect(await screen.findByText("No proven trades right now. 2 ideas are being watched.")).toBeInTheDocument();
    expect(screen.queryByText("Jets at Bills")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show all" }));
    expect(screen.getByText("Jets at Bills")).toBeInTheDocument();
    expect(screen.getAllByText(/WATCHING/).length).toBeGreaterThan(0);
  });

  it("shows the calibrated hit next to price and edge, with the raw estimate on hover", async () => {
    stubFetch({ "/api/board": [200, board([card()])] });
    renderBoard();
    const hit = await screen.findByText("31%");
    expect(hit.closest("[title]")).toHaveAttribute("title", "Model's raw estimate: 40%. Shown: what calls like this have actually hit.");
    expect(screen.getByText("25¢")).toBeInTheDocument();
    expect(screen.getByText("+4.6¢")).toBeInTheDocument();
    expect(screen.getByText("QB injury not priced · line moved 2 pts our way")).toBeInTheDocument();
  });

  it("hosted: TRADE opens the venue and offers to log the trade in the journal", async () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    const { fn } = stubFetch({ "/api/board": [200, board([card()])], "POST /api/journal": [200, { id: "j1" }] });
    renderBoard();
    await userEvent.click(await screen.findByRole("button", { name: "TRADE" }));
    expect(open).toHaveBeenCalledWith("https://kalshi.com/markets/kxnflgame", "_blank", "noopener");
    await userEvent.click(screen.getByRole("button", { name: "Log trade" }));
    await waitFor(() => {
      const post = fn.mock.calls.find(([url, init]) => url === "/api/journal" && init?.method === "POST");
      expect(post).toBeTruthy();
      expect(JSON.parse(post![1]!.body as string)).toMatchObject({ price: 0.25, card: { id: "c1" } });
    });
  });

  it("in-app: confirm stays disabled until the live preflight passes", async () => {
    let release: () => void = () => {};
    const held = new Promise<void>((r) => { release = r; });
    const inApp = board([card({ trade: { url: "x", in_app: true } })]);
    const fn = vi.fn(async (url: string) => {
      if (url === "/api/orders/preflight") {
        await held;
        return new Response(JSON.stringify({ ok: true, cost: 0.25, contracts: 10, estimated_fee: 0.18, ev_after_fees: 0.4, message: "" }));
      }
      return new Response(JSON.stringify(inApp));
    });
    vi.stubGlobal("fetch", fn);
    renderBoard();
    await userEvent.click(await screen.findByRole("button", { name: "TRADE" }));
    const confirm = screen.getByRole("button", { name: /confirm/i });
    expect(confirm).toBeDisabled();
    release();
    await waitFor(() => expect(confirm).toBeEnabled());
  });

  it("filters by the team abbreviation in ?q= from search", async () => {
    stubFetch({ "/api/board": [200, board([card(), card({ id: "c2", ticker: "KX-2", matchup: "Jets at Bills", selection: "Jets", game_id: "2026_11_NYJ_BUF" })])] });
    renderBoard("/board?q=CIN");
    expect(await screen.findByText("Bengals at Commanders")).toBeInTheDocument();
    expect(screen.queryByText("Jets at Bills")).not.toBeInTheDocument();
  });
});
