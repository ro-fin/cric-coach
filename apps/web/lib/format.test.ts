import { describe, expect, it } from "vitest";
import { formatBallCount } from "./format";

describe("formatBallCount", () => {
  it("pluralizes", () => {
    expect(formatBallCount(0)).toBe("0 balls");
    expect(formatBallCount(1)).toBe("1 ball");
    expect(formatBallCount(500)).toBe("500 balls");
  });

  it("rejects negatives and non-integers", () => {
    expect(() => formatBallCount(-1)).toThrow(/non-negative integer/);
    expect(() => formatBallCount(1.5)).toThrow(/non-negative integer/);
  });
});
