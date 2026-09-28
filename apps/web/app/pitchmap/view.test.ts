import { describe, expect, it } from "vitest";
import type { BouncePoint } from "./api";
import { degradedReasons, isEmptySession, noBounceBalls } from "./view";

const point = (ball_no: number): BouncePoint => ({
  ball_no,
  line: "off",
  length: "good",
  pitch_x: 6,
  pitch_y: 0.2,
  source: "auto",
  confidence: 0.9,
  hollow: false,
});

describe("pitch-map view model", () => {
  it("lists tagged balls without a bounce point, in tag order", () => {
    expect(noBounceBalls([4, 1, 7, 2], [point(1), point(2)])).toEqual([4, 7]);
  });

  it("builds degraded reasons from missing views or the bare flag", () => {
    expect(degradedReasons({ degraded: true, missing_views: ["C2", "C4"] })).toEqual([
      "missing camera view: C2",
      "missing camera view: C4",
    ]);
    expect(degradedReasons({ degraded: true, missing_views: [] })).toEqual([
      "session is marked degraded",
    ]);
    expect(degradedReasons({ degraded: false, missing_views: [] })).toEqual([]);
  });

  it("is empty only with no placed balls and no tags", () => {
    expect(isEmptySession(0, [])).toBe(true);
    expect(isEmptySession(0, [3])).toBe(false);
    expect(isEmptySession(2, [])).toBe(false);
  });
});
