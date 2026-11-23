import { describe, expect, it } from "vitest";
import { freshness } from "./freshness";

describe("freshness", () => {
  const now = 1_800_000_000;
  it("is LIVE inside the max age", () => expect(freshness(now - 30, 60, now)).toBe("LIVE"));
  it("is STALE past the max age", () => expect(freshness(now - 61, 60, now)).toBe("STALE"));
  it("treats an unknown as-of as STALE, never LIVE", () => expect(freshness(null, 60, now)).toBe("STALE"));
});
