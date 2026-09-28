import { describe, expect, it } from "vitest";
import { session } from "@/test/fixtures";
import {
  STEPS,
  afterCreate,
  emptyDetails,
  localIsoDate,
  resumeStep,
  stepsFor,
  toSessionIn,
  validateDetails,
} from "./flow";

const base = emptyDetails("p1", "2026-09-28");

describe("steps", () => {
  it("shows the safety check only for machine sessions", () => {
    expect(stepsFor("machine")).toBe(STEPS);
    expect(stepsFor("human").map((step) => step.id)).toEqual([
      "details",
      "cameras",
      "recording",
      "done",
    ]);
  });

  it("goes from details to the safety check or straight to cameras", () => {
    expect(afterCreate("machine")).toBe("checklist");
    expect(afterCreate("coach")).toBe("cameras");
  });

  it("resumes from the server's lifecycle state", () => {
    expect(resumeStep(session({ bowler_source: "machine" }), "created")).toBe("checklist");
    expect(resumeStep(session({ bowler_source: "human" }), "created")).toBe("cameras");
    expect(resumeStep(session(), "recording")).toBe("recording");
    expect(resumeStep(session(), "captured")).toBe("done");
    expect(resumeStep(session(), "failed")).toBe("done");
  });
});

describe("validateDetails", () => {
  it("requires a machine speed inside the server's range", () => {
    expect(validateDetails(base).speedKph).toMatch(/30 to 160/);
    expect(validateDetails({ ...base, speedKph: "29" }).speedKph).toBeDefined();
    expect(validateDetails({ ...base, speedKph: "161" }).speedKph).toBeDefined();
    expect(validateDetails({ ...base, speedKph: "abc" }).speedKph).toBeDefined();
    expect(validateDetails({ ...base, speedKph: "95" })).toEqual({});
  });

  it("ignores machine fields for other bowlers", () => {
    expect(validateDetails({ ...base, bowlerSource: "human", variation: "x".repeat(99) })).toEqual(
      {},
    );
  });

  it("checks player, date, variation and notes", () => {
    const errors = validateDetails({
      ...base,
      playerId: "",
      date: "",
      speedKph: "90",
      variation: "x".repeat(65),
      notes: "x".repeat(4001),
    });
    expect(Object.keys(errors).sort()).toEqual(["date", "notes", "playerId", "variation"]);
  });
});

describe("toSessionIn", () => {
  it("sends machine settings with a trimmed optional variation", () => {
    expect(
      toSessionIn({ ...base, speedKph: "95.5", length: "full", variation: " googly ", notes: " " }),
    ).toEqual({
      player_id: "p1",
      date: "2026-09-28",
      session_type: "batting",
      bowler_source: "machine",
      machine_settings: { speed_kph: 95.5, length: "full", variation: "googly" },
      notes: null,
    });
    expect(toSessionIn({ ...base, speedKph: "90" }).machine_settings?.variation).toBeNull();
  });

  it("sends no machine settings for a human bowler and keeps notes", () => {
    const payload = toSessionIn({ ...base, bowlerSource: "human", notes: " cover drives " });
    expect(payload.machine_settings).toBeNull();
    expect(payload.notes).toBe("cover drives");
  });
});

describe("localIsoDate", () => {
  it("formats the local calendar date with padding", () => {
    expect(localIsoDate(new Date(2026, 0, 5, 23, 59))).toBe("2026-01-05");
  });
});
