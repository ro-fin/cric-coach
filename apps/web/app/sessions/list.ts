/**
 * Session-list presentation helpers (US-B5: list filterable by date/type).
 *
 * Date filters go to the sessions API (date_from/date_to); the API exposes no
 * session_type query parameter, so the type filter narrows the fetched page
 * client-side (pure list filtering — never metric computation).
 */

import type { BadgeTone } from "@/components/ui";
import type { BowlerSource, SessionOut, SessionPage, SessionState, SessionType } from "@/lib/api";

export const TYPE_OPTIONS: readonly SessionType[] = ["batting", "bowling", "mixed"];

/** Narrow a fetched page to one session type; "" keeps every session. */
export function filterByType(items: SessionOut[], type: SessionType | ""): SessionOut[] {
  return type === "" ? items : items.filter((session) => session.session_type === type);
}

/** Badge tone per pipeline state: done is green, failed is red, in-flight is blue. */
export function stateTone(state: SessionState): BadgeTone {
  switch (state) {
    case "analyzed":
      return "success";
    case "failed":
      return "danger";
    case "created":
      return "neutral";
    default:
      return "info";
  }
}

/** Human label for the capture's bowler source. */
export const BOWLER_LABELS: Record<BowlerSource, string> = {
  machine: "Bowling machine",
  human: "Bowler",
  coach: "Coach feeds",
};

/** The degraded reasons a session carries, verbatim: one line per missing view. */
export function degradedReasons(session: SessionOut): string[] {
  if (!session.degraded) {
    return [];
  }
  return session.missing_views.length === 0
    ? ["Capture is degraded"]
    : session.missing_views.map((view) => `Missing view: ${view}`);
}

export interface PageWindow {
  first: number;
  last: number;
  hasPrevious: boolean;
  hasNext: boolean;
}

/** Which rows of `total` this page holds (1-based, inclusive) and where paging can go. */
export function pageWindow(page: SessionPage): PageWindow {
  return {
    first: page.items.length === 0 ? 0 : page.offset + 1,
    last: page.offset + page.items.length,
    hasPrevious: page.offset > 0,
    hasNext: page.offset + page.items.length < page.total,
  };
}
