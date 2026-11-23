import { afterEach, describe, expect, it, vi } from "vitest";
import { apiGet, demoUrl, UnauthorizedError } from "./api";

afterEach(() => { vi.unstubAllGlobals(); window.history.pushState({}, "", "/"); });

describe("apiGet", () => {
  it("throws UnauthorizedError on 401 so the app can drop to the login screen", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 401 })));
    await expect(apiGet("/strategies")).rejects.toBeInstanceOf(UnauthorizedError);
  });

  it("reads live data from /api with the session cookie", async () => {
    const fetchMock = vi.fn(async () => new Response('{"ok":1}', { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    expect(await apiGet("/slate")).toEqual({ ok: 1 });
    expect(fetchMock).toHaveBeenCalledWith("/api/slate", { credentials: "include" });
  });

  it("in demo mode reads the committed snapshot and never touches /api", async () => {
    window.history.pushState({}, "", "/demo/board");
    const fetchMock = vi.fn(async () => new Response('{"demo":1}', { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await apiGet("/status/jobs");
    expect(fetchMock).toHaveBeenCalledWith("/static/terminal/demo/status_jobs.json");
  });

  it("maps paths to snapshot files", () => {
    expect(demoUrl("/strategies")).toBe("/static/terminal/demo/strategies.json");
    expect(demoUrl("/status/jobs")).toBe("/static/terminal/demo/status_jobs.json");
  });
});
