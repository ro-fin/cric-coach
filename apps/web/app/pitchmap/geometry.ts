/**
 * Pitch-map drawing geometry (US-K2).
 *
 * Canonical physical frame (matches the heatmap/bounce APIs and
 * `cricai_vision.zones`): `pitch_x` is metres of bounce distance from the
 * striker's stumps toward the bowler, `pitch_y` is lateral offset where
 * +y is the OFF side for a RIGHT-hand batter. Zone classes returned by the
 * API are already handedness-resolved (a left-hander's "off" channel sits at
 * negative physical y), so drawing a cell for a left-hander mirrors the
 * channel edges — never the bounce points, which are physical coordinates.
 *
 * The zone edge values mirror the server defaults in
 * `cricai_vision.zones.DEFAULT_*`. They place cells for DRAWING only — every
 * class label and metric rendered comes from the API (US-K4 data parity).
 */

export type LineKey = "outside_off" | "off" | "middle" | "leg";
export type LengthKey = "yorker" | "full" | "good" | "short";
export type HandednessKey = "right" | "left";

/** Which end the viewer stands behind. In the batting-end frame the bowler's
 * end is the far side (top); the bowling-end frame flips both axes. */
export type EndFrame = "batting_end" | "bowling_end";

export const PITCH_LENGTH_M = 20.12; // striker's stumps -> bowler's stumps
export const PITCH_WIDTH_M = 3.05;
export const HALF_WIDTH_M = PITCH_WIDTH_M / 2;
export const POPPING_CREASE_OFFSET_M = 1.22;

export const LINES: readonly LineKey[] = ["outside_off", "off", "middle", "leg"];
export const LENGTHS: readonly LengthKey[] = ["yorker", "full", "good", "short"];

/** Server-default length bands (m from striker stumps), drawing only. */
export const LENGTH_BANDS_M: Readonly<Record<LengthKey, readonly [number, number]>> = {
  yorker: [0, 2],
  full: [2, 5],
  good: [5, 8],
  short: [8, Number.POSITIVE_INFINITY],
};

/** Server-default line channels in batter-relative y (+ = off side), drawing only. */
export const LINE_CHANNELS_M: Readonly<Record<LineKey, readonly [number, number]>> = {
  leg: [Number.NEGATIVE_INFINITY, -0.1143],
  middle: [-0.1143, 0.1143],
  off: [0.1143, 0.4],
  outside_off: [0.4, Number.POSITIVE_INFINITY],
};

// Lateral axis is stretched relative to length so 16 cells stay clickable;
// both axes are labelled so the stretch is honest.
export const X_SCALE = 60; // px per lateral metre
export const Y_SCALE = 22; // px per down-pitch metre
export const MARGIN = 40; // px of slack: labels + off-pitch bounce points
export const SVG_WIDTH = 2 * MARGIN + PITCH_WIDTH_M * X_SCALE;
export const SVG_HEIGHT = 2 * MARGIN + PITCH_LENGTH_M * Y_SCALE;

export interface SvgPoint {
  u: number;
  v: number;
}

export interface SvgRect {
  u: number;
  v: number;
  w: number;
  h: number;
}

/** Project a physical pitch point to SVG px for the chosen end frame.
 * Points are NEVER clamped: off-pitch bounces render at their true spot. */
export function project(frame: EndFrame, xM: number, yM: number): SvgPoint {
  if (frame === "batting_end") {
    return {
      u: MARGIN + (yM + HALF_WIDTH_M) * X_SCALE,
      v: MARGIN + (PITCH_LENGTH_M - xM) * Y_SCALE,
    };
  }
  return {
    u: MARGIN + (HALF_WIDTH_M - yM) * X_SCALE,
    v: MARGIN + xM * Y_SCALE,
  };
}

function clampX(xM: number): number {
  return Math.min(Math.max(xM, 0), PITCH_LENGTH_M);
}

function clampY(yM: number): number {
  return Math.min(Math.max(yM, -HALF_WIDTH_M), HALF_WIDTH_M);
}

/** Drawing rect for one zone cell: infinite bands clip to the pitch, line
 * channels mirror for left-hand batters, corners project per frame. */
export function cellRect(
  frame: EndFrame,
  handedness: HandednessKey,
  line: LineKey,
  length: LengthKey,
): SvgRect {
  const [bandLo, bandHi] = LENGTH_BANDS_M[length];
  const [relLo, relHi] = LINE_CHANNELS_M[line];
  const yLo = clampY(handedness === "right" ? relLo : -relHi);
  const yHi = clampY(handedness === "right" ? relHi : -relLo);
  const a = project(frame, clampX(bandLo), yLo);
  const b = project(frame, clampX(bandHi), yHi);
  return {
    u: Math.min(a.u, b.u),
    v: Math.min(a.v, b.v),
    w: Math.abs(a.u - b.u),
    h: Math.abs(a.v - b.v),
  };
}

/** Screen side of the batter's off side: batting-end + right-hand = right;
 * flipping either the frame or the handedness flips the side. */
export function offSideIsRight(frame: EndFrame, handedness: HandednessKey): boolean {
  return (frame === "batting_end") === (handedness === "right");
}

// Viridis anchors (parity with the server PNG's colormap).
const VIRIDIS_ANCHORS: readonly (readonly [number, number, number])[] = [
  [68, 1, 84],
  [59, 82, 139],
  [33, 145, 140],
  [94, 201, 98],
  [253, 231, 37],
];

/** Sequential viridis-like ramp over t in [0, 1] (clamped). */
export function viridis(t: number): string {
  const clamped = Math.min(Math.max(t, 0), 1);
  const scaled = clamped * (VIRIDIS_ANCHORS.length - 1);
  const idx = Math.min(Math.floor(scaled), VIRIDIS_ANCHORS.length - 2);
  const frac = scaled - idx;
  const [r0, g0, b0] = VIRIDIS_ANCHORS[idx];
  const [r1, g1, b1] = VIRIDIS_ANCHORS[idx + 1];
  const lerp = (a: number, b: number): number => Math.round(a + (b - a) * frac);
  return `rgb(${lerp(r0, r1)}, ${lerp(g0, g1)}, ${lerp(b0, b1)})`;
}

/** Neutral fill for cells whose shading metric is unknowable (no tagged balls). */
export const NO_DATA_FILL = "#9ca3af";

/** Density shading: share of the busiest cell (0 when the map is empty). */
export function densityT(count: number, peak: number): number {
  return peak > 0 ? count / peak : 0;
}

/** Count-label colour flips for contrast: viridis runs dark -> bright. */
export function labelColor(t: number): string {
  return t > 0.5 ? "#000000" : "#ffffff";
}
