import { describe, expect, it } from "vitest";
import type { SessionOut } from "@/lib/api";
import { TYPE_OPTIONS, filterByType, sessionLabel } from "./list";

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

describe("sessionLabel", () => {
  it("renders date, type, bowler source and state", () => {
    expect(sessionLabel(session())).toBe("2026-07-09 · batting · machine · analyzed");
  });

  it("flags degraded captures loudly", () => {
    expect(sessionLabel(session({ degraded: true }))).toBe(
      "2026-07-09 · batting · machine · analyzed · degraded",
    );
  });
});
