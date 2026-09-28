/** US-K2 UT: handedness mirroring + frame-of-reference switching math. */

import { describe, expect, it } from "vitest";
import {
  cellRect,
  densityT,
  HALF_WIDTH_M,
  labelColor,
  MARGIN,
  NO_DATA_FILL,
  offSideIsRight,
  PITCH_LENGTH_M,
  project,
  SVG_HEIGHT,
  SVG_WIDTH,
  viridis,
  X_SCALE,
  Y_SCALE,
} from "./geometry";

describe("project", () => {
  it("puts the striker stumps at the bottom and the bowler end far in the batting-end frame", () => {
    const striker = project("batting_end", 0, 0);
    const bowler = project("batting_end", PITCH_LENGTH_M, 0);
    expect(striker.v).toBeCloseTo(MARGIN + PITCH_LENGTH_M * Y_SCALE);
    expect(bowler.v).toBeCloseTo(MARGIN); // bowler end far side (top)
    expect(striker.u).toBeCloseTo(MARGIN + HALF_WIDTH_M * X_SCALE); // centre line
  });

  it("flips both axes in the bowling-end frame", () => {
    const striker = project("bowling_end", 0, 0);
    const bowler = project("bowling_end", PITCH_LENGTH_M, 0);
    expect(striker.v).toBeCloseTo(MARGIN); // batter end far side now
    expect(bowler.v).toBeCloseTo(MARGIN + PITCH_LENGTH_M * Y_SCALE);
  });

  it("mirrors the lateral axis between frames: physical +y is right at the batting end, left at the bowling end", () => {
    const battingEnd = project("batting_end", 10, 0.4);
    const bowlingEnd = project("bowling_end", 10, 0.4);
    const centre = MARGIN + HALF_WIDTH_M * X_SCALE;
    expect(battingEnd.u).toBeCloseTo(centre + 0.4 * X_SCALE);
    expect(bowlingEnd.u).toBeCloseTo(centre - 0.4 * X_SCALE);
  });

  it("never clamps off-pitch points (lofted / mis-mapped bounces render true, T7)", () => {
    const beyond = project("batting_end", 21.5, 0);
    expect(beyond.v).toBeCloseTo(MARGIN + (PITCH_LENGTH_M - 21.5) * Y_SCALE);
    expect(beyond.v).toBeLessThan(MARGIN); // outside the pitch rectangle, still projected
  });
});

describe("cellRect", () => {
  it("places the off channel on the physical +y side for a right-hand batter", () => {
    const rect = cellRect("batting_end", "right", "off", "good");
    expect(rect.u).toBeCloseTo(MARGIN + (0.1143 + HALF_WIDTH_M) * X_SCALE);
    expect(rect.w).toBeCloseTo((0.4 - 0.1143) * X_SCALE);
    expect(rect.v).toBeCloseTo(MARGIN + (PITCH_LENGTH_M - 8) * Y_SCALE);
    expect(rect.h).toBeCloseTo(3 * Y_SCALE); // good band is 5-8 m
  });

  it("mirrors line channels for a left-hand batter (US-K2 AC: handedness respected)", () => {
    const rect = cellRect("batting_end", "left", "off", "good");
    expect(rect.u).toBeCloseTo(MARGIN + (-0.4 + HALF_WIDTH_M) * X_SCALE);
    expect(rect.w).toBeCloseTo((0.4 - 0.1143) * X_SCALE);
  });

  it("clips infinite bands to the pitch rectangle", () => {
    const short = cellRect("batting_end", "right", "leg", "short"); // both bands infinite
    expect(short.v).toBeCloseTo(MARGIN); // short band caps at the bowler stumps
    expect(short.h).toBeCloseTo((PITCH_LENGTH_M - 8) * Y_SCALE);
    expect(short.u).toBeCloseTo(MARGIN); // leg channel caps at the pitch edge
    expect(short.w).toBeCloseTo((HALF_WIDTH_M - 0.1143) * X_SCALE);
  });

  it("keeps rects normalized in the flipped bowling-end frame", () => {
    const rect = cellRect("bowling_end", "right", "outside_off", "yorker");
    expect(rect.w).toBeGreaterThan(0);
    expect(rect.h).toBeGreaterThan(0);
    expect(rect.v).toBeCloseTo(MARGIN); // yorker band [0,2] sits at the far (striker) end
    expect(rect.u).toBeCloseTo(MARGIN); // outside-off is screen-left for RH from the bowling end
  });
});

describe("offSideIsRight", () => {
  it("resolves all four frame x handedness combinations", () => {
    expect(offSideIsRight("batting_end", "right")).toBe(true);
    expect(offSideIsRight("batting_end", "left")).toBe(false);
    expect(offSideIsRight("bowling_end", "right")).toBe(false);
    expect(offSideIsRight("bowling_end", "left")).toBe(true);
  });
});

describe("color scales", () => {
  it("hits the viridis anchors at the extremes and clamps out-of-range t", () => {
    expect(viridis(0)).toBe("rgb(68, 1, 84)");
    expect(viridis(1)).toBe("rgb(253, 231, 37)");
    expect(viridis(-0.5)).toBe("rgb(68, 1, 84)");
    expect(viridis(2)).toBe("rgb(253, 231, 37)");
  });

  it("interpolates between anchors", () => {
    expect(viridis(0.125)).toBe(`rgb(${64}, ${42}, ${112})`); // halfway 0 -> 0.25
  });

  it("scales density against the busiest cell, safely when the map is empty", () => {
    expect(densityT(3, 6)).toBeCloseTo(0.5);
    expect(densityT(0, 0)).toBe(0);
  });

  it("flips the count-label colour for bright cells", () => {
    expect(labelColor(0.4)).toBe("#ffffff");
    expect(labelColor(0.9)).toBe("#000000");
  });

  it("exposes a neutral no-data fill distinct from the ramp", () => {
    expect(NO_DATA_FILL).toBe("#9ca3af");
  });
});

describe("canvas dimensions", () => {
  it("derives the svg size from the pitch and margins", () => {
    expect(SVG_WIDTH).toBeCloseTo(2 * MARGIN + 3.05 * X_SCALE);
    expect(SVG_HEIGHT).toBeCloseTo(2 * MARGIN + PITCH_LENGTH_M * Y_SCALE);
  });
});
