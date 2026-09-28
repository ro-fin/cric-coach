import { describe, expect, it } from "vitest";
import type { SessionOut } from "@/lib/api";
import {
  BOWLER_LABELS,
  TYPE_OPTIONS,
  degradedReasons,
  filterByType,
  pageWindow,
  stateTone,
} from "./list";

function session(overrides: Partial<SessionOut> = {}): SessionOut {
  return {
    id: "s1",
    player_id: "p1",
    session_date: "2026-07-09",
    session_type: "batting",
    bowler_source: "machine",
    machine_settings: null,
    notes: null,
    state: "analyzed",
    degraded: false,
    missing_views: [],
    ...overrides,
  };
}

describe("filterByType (US-B5: list filterable by type)", () => {
  const items = [
    session({ id: "a", session_type: "batting" }),
    session({ id: "b", session_type: "bowling" }),
    session({ id: "c", session_type: "mixed" }),
  ];

  it("keeps everything for the empty filter", () => {
    expect(filterByType(items, "")).toBe(items);
  });

  it("narrows to one type", () => {
    expect(filterByType(items, "bowling").map((s) => s.id)).toEqual(["b"]);
  });

  it("pins the three session types as options", () => {
    expect(TYPE_OPTIONS).toEqual(["batting", "bowling", "mixed"]);
  });
});

describe("stateTone", () => {
  it("maps every pipeline state to a badge tone", () => {
    expect(stateTone("analyzed")).toBe("success");
    expect(stateTone("failed")).toBe("danger");
    expect(stateTone("created")).toBe("neutral");
    expect(stateTone("recording")).toBe("info");
    expect(stateTone("captured")).toBe("info");
    expect(stateTone("processing")).toBe("info");
  });
});

describe("BOWLER_LABELS", () => {
  it("labels every bowler source", () => {
    expect(Object.keys(BOWLER_LABELS).sort()).toEqual(["coach", "human", "machine"]);
  });
});

describe("degradedReasons", () => {
  it("is empty for a healthy capture", () => {
    expect(degradedReasons(session())).toEqual([]);
  });

  it("names each missing view verbatim", () => {
    expect(degradedReasons(session({ degraded: true, missing_views: ["cam_side"] }))).toEqual([
      "Missing view: cam_side",
    ]);
  });

  it("still says degraded when no view is named", () => {
    expect(degradedReasons(session({ degraded: true }))).toEqual(["Capture is degraded"]);
  });
});

describe("pageWindow", () => {
  const page = (offset: number, count: number, total: number) => ({
    items: Array.from({ length: count }, (_, i) => session({ id: `s${i}` })),
    total,
    limit: 20,
    offset,
  });

  it("covers a middle page", () => {
    expect(pageWindow(page(20, 20, 60))).toEqual({
      first: 21,
      last: 40,
      hasPrevious: true,
      hasNext: true,
    });
  });

  it("covers the only page and an empty page", () => {
    expect(pageWindow(page(0, 3, 3))).toEqual({
      first: 1,
      last: 3,
      hasPrevious: false,
      hasNext: false,
    });
    expect(pageWindow(page(0, 0, 0)).first).toBe(0);
  });
});
