/** Per-report device cache (US-K5 offline AC).
 *
 * Entries are keyed by report id (`cricai.report.<id>`) and carry a
 * `cachedAt` timestamp so the offline banner can state how stale the copy
 * is. A request for report B can therefore never be answered with report A,
 * and a corrupt entry reads as an honest cache miss (and is removed) instead
 * of wedging the page on JSON.parse.
 *
 * Only PUBLISHED reports are ever replayed offline (US-G3/H5): a cached entry
 * whose report is no longer published — a draft/blocked copy from before a
 * report was withdrawn, or a pre-existing entry — reads as a miss and is
 * healed away, so a shared device can never replay an unpublished report.
 *
 * Lives outside page.tsx because Next.js pages may only export page fields —
 * a named export from the page module fails the production build.
 */

import type { Report } from "./api";

export interface CachedReport {
  /** ISO-8601 instant the copy was saved on this device. */
  cachedAt: string;
  report: Report;
}

/** localStorage key for one report's cached copy. */
export function reportCacheKey(reportId: string): string {
  return `cricai.report.${reportId}`;
}

function isCachedReport(value: unknown): value is CachedReport {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { cachedAt?: unknown }).cachedAt === "string" &&
    typeof (value as { report?: unknown }).report === "object" &&
    (value as { report?: unknown }).report !== null
  );
}

/** Cache a fetched report under its own id with a saved-at timestamp. */
export function writeCachedReport(report: Report): CachedReport {
  const entry: CachedReport = { cachedAt: new Date().toISOString(), report };
  window.localStorage.setItem(reportCacheKey(report.id), JSON.stringify(entry));
  return entry;
}

/** The cached copy of exactly this report, or null (corrupt = miss + heal). */
export function readCachedReport(reportId: string): CachedReport | null {
  const raw = window.localStorage.getItem(reportCacheKey(reportId));
  if (raw === null) {
    return null;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    window.localStorage.removeItem(reportCacheKey(reportId));
    return null;
  }
  if (!isCachedReport(parsed)) {
    window.localStorage.removeItem(reportCacheKey(reportId));
    return null;
  }
  if (parsed.report.status !== "published") {
    // A report withdrawn (or never published) after it was cached must not
    // replay: treat it as a miss and heal the key (US-G3/H5).
    window.localStorage.removeItem(reportCacheKey(reportId));
    return null;
  }
  return parsed;
}
