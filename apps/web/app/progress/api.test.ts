import { afterEach, describe, expect, it, vi } from "vitest";
import {
  type ApiConfig,
  DEFAULT_CONFIG,
  fetchMilestones,
  fetchRollupReports,
  fetchWellnessState,
  fetchWorkloadSummary,
  loadDashboard,
  newestReport,
} from "./api";
import type { RollupReport } from "./types";
import { defaultConfig } from "@/lib/api";

const config: ApiConfig = { base: "http://api.test", token: "tok-1" };

function ok(payload: unknown) {
  return { ok: true, status: 200, json: async () => payload };
}

function report(kind: "weekly" | "monthly", id: string): RollupReport {
  return {
    id,
    kind,
    status: "published",
    period_start: "2026-06-15",
    period_end: "2026-06-21",
    body: {
      kind,
      period: { start: "2026-06-15", end: "2026-06-21" },
      positive: "p",
      honesty_banner: null,
      trends: [],
      milestones: [],
    },
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("fetch helpers", () => {
  it("hit the pinned endpoints with the bearer token", async () => {
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<unknown>>(
      async () => ok([]),
    );
    vi.stubGlobal("fetch", fetchMock);

    await fetchRollupReports(config, "p1", "weekly");
    await fetchMilestones(config, "p1");
    await fetchWorkloadSummary(config, "p1");
    await fetchWellnessState(config, "p1");

    const urls = fetchMock.mock.calls.map((call) => call[0]);
    expect(urls).toEqual([
      "http://api.test/reports?player_id=p1&kind=weekly",
      "http://api.test/milestones/players/p1",
      "http://api.test/workload/players/p1/summary",
      "http://api.test/wellness/p1/state",
    ]);
    for (const call of fetchMock.mock.calls) {
      expect(call[1]).toEqual({ headers: { Authorization: "Bearer tok-1" } });
    }
  });

  it("throws on a non-ok response, naming the path and status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 404, json: async () => ({}) })),
    );
    await expect(fetchMilestones(config, "nope")).rejects.toThrow(
      "GET /milestones/players/nope failed: 404",
    );
  });
});

describe("newestReport", () => {
  it("takes the first (newest-period) row and null when empty", () => {
    const first = report("weekly", "r1");
    expect(newestReport([first, report("weekly", "r2")])).toBe(first);
    expect(newestReport([])).toBeNull();
  });
});

describe("loadDashboard", () => {
  it("assembles the dashboard from all five surfaces", async () => {
    const weekly = report("weekly", "rw");
    const monthly = report("monthly", "rm");
    const window = {
      window_start: "2026-06-11",
      window_end: "2026-06-17",
      weighted_balls: 81,
      weighted_overs: 13.5,
      bowling_days: ["2026-06-15"],
      ceiling_overs: 16,
      violations: [],
      remaining_balls: 15,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.includes("kind=weekly")) return ok([weekly]);
        if (url.includes("kind=monthly")) return ok([monthly]);
        if (url.includes("/milestones/")) return ok([]);
        if (url.includes("/workload/")) return ok([window]);
        return ok({ pain_active: true });
      }),
    );
    expect(await loadDashboard(config, "p1")).toEqual({
      weekly,
      monthly,
      milestones: [],
      workload: window,
      wellness: { pain_active: true },
    });
  });

  it("returns nulls when the lab has no reports or workload yet", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url.includes("/wellness/") ? ok({ pain_active: false }) : ok([]),
      ),
    );
    expect(await loadDashboard(config, "p1")).toEqual({
      weekly: null,
      monthly: null,
      milestones: [],
      workload: null,
      wellness: { pain_active: false },
    });
  });
});

describe("DEFAULT_CONFIG", () => {
  it("mirrors the shared lib/api defaults (same before and after the proxy switch)", () => {
    const shared = defaultConfig();
    expect(DEFAULT_CONFIG).toEqual({ base: shared.baseUrl, token: shared.token });
  });

  it("sends no Authorization header when the shared token is empty", async () => {
    const fetchMock = vi.fn(async () => ok([]));
    vi.stubGlobal("fetch", fetchMock);
    await fetchMilestones({ base: "/api/cricai", token: "" }, "p1");
    expect(fetchMock).toHaveBeenCalledWith("/api/cricai/milestones/players/p1", { headers: {} });
  });
});
