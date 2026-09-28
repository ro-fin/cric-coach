import { describe, expect, it } from "vitest";
import { dailyReport, player, reportBody, workloadWindow } from "@/test/fixtures.core";
import {
  canStartSession,
  evidenceLinks,
  latestPublished,
  pickPlayer,
  playerIdFromSearch,
  reportCaveats,
  safetyLabel,
  workloadTone,
} from "./view";

describe("pickPlayer", () => {
  const players = [player({ id: "a" }), player({ id: "b" })];

  it("honours a requested id that is listed", () => {
    expect(pickPlayer(players, "b")?.id).toBe("b");
  });

  it("falls back to the first player for an unknown or missing id", () => {
    expect(pickPlayer(players, "zzz")?.id).toBe("a");
    expect(pickPlayer(players, null)?.id).toBe("a");
  });

  it("returns null when there are no players", () => {
    expect(pickPlayer([], "a")).toBeNull();
  });
});

describe("playerIdFromSearch", () => {
  it("reads ?player= and treats blank as absent", () => {
    expect(playerIdFromSearch("?player=p9")).toBe("p9");
    expect(playerIdFromSearch("?player=")).toBeNull();
    expect(playerIdFromSearch("")).toBeNull();
  });
});

describe("latestPublished", () => {
  it("skips drafts and blocked reports and keeps API order", () => {
    const reports = [
      dailyReport({ id: "d", status: "draft" }),
      dailyReport({ id: "b", status: "blocked" }),
      dailyReport({ id: "new", status: "published" }),
      dailyReport({ id: "old", status: "published" }),
    ];
    expect(latestPublished(reports)?.id).toBe("new");
  });

  it("returns null when nothing is published", () => {
    expect(latestPublished([dailyReport({ status: "draft" })])).toBeNull();
    expect(latestPublished([])).toBeNull();
  });
});

describe("evidenceLinks", () => {
  const evidence = {
    "12": { cam_front: "c3" },
    "7": { cam_side: "c2", cam_front: "c1" },
  };

  it("orders balls numerically, then cameras, and deep-links ?ball=N", () => {
    expect(evidenceLinks(evidence, "s1")).toEqual([
      { key: "7-cam_front", label: "Ball 7 · cam_front", href: "/sessions/s1?ball=7" },
      { key: "7-cam_side", label: "Ball 7 · cam_side", href: "/sessions/s1?ball=7" },
      { key: "12-cam_front", label: "Ball 12 · cam_front", href: "/sessions/s1?ball=12" },
    ]);
  });

  it("gives no href when the report has no session", () => {
    expect(evidenceLinks({ "1": { cam: "c" } }, null)[0].href).toBeNull();
  });
});

describe("reportCaveats", () => {
  it("returns the honesty banner then the coverage note, verbatim", () => {
    const report = dailyReport({
      body: reportBody({ honesty_banner: "Only 1 camera.", coverage_note: "20 of 60 balls." }),
    });
    expect(reportCaveats(report)).toEqual(["Only 1 camera.", "20 of 60 balls."]);
  });

  it("drops null and empty entries", () => {
    const report = dailyReport({ body: reportBody({ honesty_banner: "", coverage_note: null }) });
    expect(reportCaveats(report)).toEqual([]);
  });
});

describe("workload helpers", () => {
  it("labels every safety code", () => {
    expect(safetyLabel("workload_ceiling")).toMatch(/ceiling/);
    expect(safetyLabel("day_pattern_violation")).toMatch(/days/);
    expect(safetyLabel("pain_flag")).toMatch(/Pain/);
  });

  it("is danger with any violation, success otherwise", () => {
    expect(workloadTone(workloadWindow())).toBe("success");
    expect(workloadTone(workloadWindow({ violations: ["pain_flag"] }))).toBe("danger");
  });
});

describe("canStartSession", () => {
  it("allows parent and coach only", () => {
    expect(canStartSession("parent")).toBe(true);
    expect(canStartSession("coach")).toBe(true);
    expect(canStartSession("player")).toBe(false);
    expect(canStartSession(null)).toBe(false);
  });
});
