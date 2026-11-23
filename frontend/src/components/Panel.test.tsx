import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Panel } from "./Panel";

describe("Panel", () => {
  it("shows title, source, status chip and as-of time", () => {
    const asOf = new Date(2026, 10, 23, 14, 5).getTime() / 1000;
    render(<Panel title="NFL Slate" source="nflverse" status="STALE" asOf={asOf}>body</Panel>);
    expect(screen.getByText("NFL Slate")).toBeInTheDocument();
    expect(screen.getByText("nflverse")).toBeInTheDocument();
    expect(screen.getByText("STALE")).toBeInTheDocument();
    expect(screen.getByText("14:05")).toBeInTheDocument();
    expect(screen.getByText("body")).toBeInTheDocument();
  });

  it("shows the error message instead of the body on ERROR", () => {
    render(<Panel title="Jobs" source="api" status="ERROR" asOf={null} error="timed out">body</Panel>);
    expect(screen.getByText("timed out")).toBeInTheDocument();
    expect(screen.queryByText("body")).not.toBeInTheDocument();
  });
});
