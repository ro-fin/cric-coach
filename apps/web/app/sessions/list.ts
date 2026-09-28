/**
 * Session-list presentation helpers (US-B5: list filterable by date/type).
 *
 * Date filters go to the sessions API (date_from/date_to); the API exposes no
 * session_type query parameter, so the type filter narrows the fetched page
 * client-side (pure list filtering — never metric computation).
 */

import type { SessionOut, SessionType } from "@/lib/api";

export const TYPE_OPTIONS: readonly SessionType[] = ["batting", "bowling", "mixed"];

/** Narrow a fetched page to one session type; "" keeps every session. */
export function filterByType(items: SessionOut[], type: SessionType | ""): SessionOut[] {
  return type === "" ? items : items.filter((session) => session.session_type === type);
}

/** One-line list label for a session row. */
export function sessionLabel(session: SessionOut): string {
  const base = `${session.session_date} · ${session.session_type} · ${session.bowler_source} · ${session.state}`;
  return session.degraded ? `${base} · degraded` : base;
}
