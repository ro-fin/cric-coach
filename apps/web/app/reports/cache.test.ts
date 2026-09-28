/** US-K5 offline cache: per-report keys, {cachedAt, report} entries, and
 * corrupt/foreign-shaped entries treated as an honest cache miss (self-healed)
 * so the page can never wedge on JSON.parse or replay a mangled report. */

import { beforeEach, describe, expect, it } from "vitest";
import type { Report } from "./api";
import { readCachedReport, reportCacheKey, writeCachedReport } from "./cache";

function report(id: string): Report {
  return {
    id,
    player_id: "p1",
    session_id: "s1",
    kind: "daily",
    period_start: "2026-07-09",
    period_end: "2026-07-09",
    status: "published",
    body: {
      kind: "daily",
      period: { start: "2026-07-09", end: "2026-07-09" },
      main_correction: null,
      drill: null,
      goal: null,
      secondary: [],
      positive: "Great effort today.",
      safety: null,
      honesty_banner: null,
      coverage_note: null,
      fatigue_note: null,
      claims: [],
    },
    created_at: "2026-07-09T18:00:00Z",
  };
}

/** In-memory Storage: the test runtime's window.localStorage lacks the API. */
function stubLocalStorage(): void {
  const store = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, value),
      removeItem: (key: string) => void store.delete(key),
      clear: () => store.clear(),
    },
  });
}

beforeEach(() => {
  stubLocalStorage();
});

describe("reportCacheKey", () => {
  it("keys the cache per report id", () => {
    expect(reportCacheKey("r1")).toBe("cricai.report.r1");
    expect(reportCacheKey("r2")).toBe("cricai.report.r2");
  });
});

describe("writeCachedReport / readCachedReport", () => {
  it("round-trips a report with its cached-at timestamp under its own key", () => {
    const written = writeCachedReport(report("r1"));
    expect(new Date(written.cachedAt).getTime()).not.toBeNaN();
    const entry = readCachedReport("r1");
    expect(entry).not.toBeNull();
    expect(entry?.report.id).toBe("r1");
    expect(entry?.cachedAt).toBe(written.cachedAt);
    // a different id is an honest miss, never a cross-report hit
    expect(readCachedReport("r2")).toBeNull();
  });

  it("returns null when nothing is cached", () => {
    expect(readCachedReport("r1")).toBeNull();
  });

  it("treats corrupt JSON as no-cache and removes the wedged key", () => {
    window.localStorage.setItem(reportCacheKey("r1"), '{"cachedAt":"2026-07-0');
    expect(readCachedReport("r1")).toBeNull();
    expect(window.localStorage.getItem(reportCacheKey("r1"))).toBeNull();
  });

  it.each([
    ["JSON null", "null"],
    ["a scalar", '"hello"'],
    ["a non-string cachedAt", '{"cachedAt":7,"report":{}}'],
    ["a missing report", '{"cachedAt":"2026-07-08T09:30:00Z"}'],
    ["a null report", '{"cachedAt":"2026-07-08T09:30:00Z","report":null}'],
  ])("treats %s as no-cache and self-heals", (_label, raw) => {
    window.localStorage.setItem(reportCacheKey("r1"), raw);
    expect(readCachedReport("r1")).toBeNull();
    expect(window.localStorage.getItem(reportCacheKey("r1"))).toBeNull();
  });

  it.each(["draft", "blocked"])(
    "treats a cached %s (non-published) report as no-cache and heals it (US-G3/H5)",
    (status) => {
      window.localStorage.setItem(
        reportCacheKey("r1"),
        JSON.stringify({ cachedAt: "2026-07-08T09:30:00Z", report: { ...report("r1"), status } }),
      );
      expect(readCachedReport("r1")).toBeNull();
      expect(window.localStorage.getItem(reportCacheKey("r1"))).toBeNull();
    },
  );
});
