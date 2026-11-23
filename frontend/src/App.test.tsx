import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { SLATE, stubFetch } from "./test-fetch";

afterEach(() => { vi.unstubAllGlobals(); window.history.pushState({}, "", "/"); });

const renderAt = (path: string) => {
  window.history.pushState({}, "", path);
  return render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);
};

describe("App shell", () => {
  it("shows the login screen when there is no session", async () => {
    stubFetch({ "/api/me": [200, { user: null }] });
    renderAt("/");
    expect(await screen.findByRole("heading", { name: /sign in/i })).toBeInTheDocument();
  });

  it("logs in and shows the terminal with all eight sections", async () => {
    let user: string | null = null;
    stubFetch({
      "/api/me": () => [200, { user }],
      "POST /api/login": () => { user = "ashar"; return [200, { user }]; },
      "/api/slate": [200, SLATE],
    });
    renderAt("/");
    await userEvent.type(await screen.findByLabelText(/username/i), "ashar");
    await userEvent.type(screen.getByLabelText(/password/i), "pw");
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }));
    const nav = await screen.findByRole("navigation");
    for (const name of ["Home", "Board", "Markets", "Sports", "Macro", "Politics", "P&L", "Research"]) {
      expect(nav).toHaveTextContent(name);
    }
  });

  it("drops back to login when a panel gets a 401 mid-session", async () => {
    stubFetch({ "/api/me": [200, { user: "ashar" }], "/api/slate": [401, {}] });
    renderAt("/");
    expect(await screen.findByRole("heading", { name: /sign in/i })).toBeInTheDocument();
  });

  it("demo mode shows the banner and never calls /api even with a live session", async () => {
    const { calls } = stubFetch({
      "/api/me": [200, { user: "ashar" }],
      "/static/terminal/demo/slate.json": [200, SLATE],
      "/static/terminal/demo/me.json": [200, { user: "demo" }],
    });
    renderAt("/demo");
    expect(await screen.findByText(/demo/i, { selector: ".demo-banner *, .demo-banner" })).toBeInTheDocument();
    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
    await act(async () => { await new Promise((r) => setTimeout(r, 20)); });
    expect(calls.filter((c) => c.startsWith("/api/"))).toEqual([]);
  });

  it("unbuilt sections say which module brings them", async () => {
    stubFetch({ "/api/me": [200, { user: "ashar" }], "/api/slate": [200, SLATE] });
    renderAt("/macro");
    expect(await screen.findByText(/module 4/i)).toBeInTheDocument();
  });
});
