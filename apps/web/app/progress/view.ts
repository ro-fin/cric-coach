/**
 * Pure view helpers for the progress page (US-K4) — kept out of page.tsx so
 * the app-router page module only exports its component.
 */

import type { TrendSeries } from "./types";

export function playerIdFromSearch(search: string): string | null {
  return new URLSearchParams(search).get("player");
}

/** Qualified regressing series — the only dips kid mode may mention, and only
 * inside a coaching frame (US-K4 kid-mode acceptance). */
export function regressingTrends(trends: TrendSeries[]): TrendSeries[] {
  return trends.filter((trend) => trend.qualified && trend.direction === "regressing");
}
