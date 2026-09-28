/**
 * Pitch-map view model (US-K2): pure helpers kept out of the component so the
 * honest-state decisions are unit-tested on their own.
 */

import type { BouncePoint, SessionInfo } from "./api";

/** Tagged balls that have no bounce point, in tag order (beamer, full toss,
 * out of frame or not yet marked). A set difference, not a metric. */
export function noBounceBalls(tagBallNos: readonly number[], points: readonly BouncePoint[]): number[] {
  const withBounce = new Set(points.map((point) => point.ball_no));
  return tagBallNos.filter((ballNo) => !withBounce.has(ballNo));
}

/** Degraded-banner reasons from the session's own honesty fields: each
 * missing camera view by its served id, or the bare degraded flag. */
export function degradedReasons(session: Pick<SessionInfo, "degraded" | "missing_views">): string[] {
  const views = session.missing_views.map((view) => `missing camera view: ${view}`);
  if (views.length > 0) {
    return views;
  }
  return session.degraded ? ["session is marked degraded"] : [];
}

/** Nothing to draw and nothing tagged: the empty state, not a blank map. */
export function isEmptySession(totalBalls: number, tagBallNos: readonly number[]): boolean {
  return totalBalls === 0 && tagBallNos.length === 0;
}
