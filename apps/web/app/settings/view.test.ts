import { describe, expect, it } from "vitest";
import { appSettings } from "@/test/fixtures.ops";
import type { AppSettings } from "./api";
import {
  allowlistKeys,
  approvalsNeeded,
  approvalText,
  draftFrom,
  draftProblems,
  SETTINGS_ROLES,
  toSettingsIn,
} from "./view";

const ACTIVE = appSettings().settings;

function withChanges(change: Partial<{ mode: "coach_gate"; enabled: boolean; allowlist: string[] }>) {
  return {
    report_review: { ...ACTIVE.report_review, mode: change.mode ?? ACTIVE.report_review.mode },
    live_mode: {
      ...ACTIVE.live_mode,
      enabled: change.enabled ?? ACTIVE.live_mode.enabled,
      allowlist: change.allowlist ?? ACTIVE.live_mode.allowlist,
    },
  } as AppSettings;
}

describe("settings draft", () => {
  it("is a parent screen", () => {
    expect(SETTINGS_ROLES).toEqual(["parent"]);
  });

  it("starts from the active version", () => {
    expect(draftFrom(ACTIVE)).toEqual({
      mode: "auto_publish",
      timeoutHours: "24",
      liveEnabled: false,
      allowlist: "ball_count\ntarget_hit_tally\nworkload_remaining_balls\nfatigue_nudge",
      reason: "",
    });
  });

  it("parses the allowlist one key per line", () => {
    expect(allowlistKeys(" a \n\nb\na\n")).toEqual(["a", "b"]);
  });

  it("requires a positive timeout and a reason", () => {
    expect(draftProblems({ ...draftFrom(ACTIVE), reason: "season" })).toEqual([]);
    expect(draftProblems({ ...draftFrom(ACTIVE), timeoutHours: "0" })).toEqual([
      "report_review.timeout_hours must be a positive number",
      "a reason is required for every settings change",
    ]);
    expect(draftProblems({ ...draftFrom(ACTIVE), timeoutHours: " ", reason: "x" })).toHaveLength(1);
    expect(draftProblems({ ...draftFrom(ACTIVE), timeoutHours: "x", reason: "x" })).toHaveLength(1);
  });

  it("rebuilds the whole version and keeps unknown keys", () => {
    const active = {
      ...ACTIVE,
      report_review: { ...ACTIVE.report_review, extra: 1 },
    } as AppSettings;
    const draft = { ...draftFrom(active), timeoutHours: "12", allowlist: "ball_count", reason: " r " };
    expect(toSettingsIn(active, draft)).toEqual({
      settings: {
        report_review: { mode: "auto_publish", timeout_hours: 12, extra: 1 },
        live_mode: { enabled: false, allowlist: ["ball_count"] },
      },
      reason: "r",
    });
  });
});

describe("approval gates", () => {
  it("needs nothing for a narrowing or unchanged version", () => {
    expect(approvalsNeeded(ACTIVE, withChanges({ allowlist: ["ball_count"] }))).toEqual({
      coach: false,
      parent: false,
    });
  });

  it("needs the coach for a review-mode change", () => {
    expect(approvalsNeeded(ACTIVE, withChanges({ mode: "coach_gate" }))).toEqual({
      coach: true,
      parent: false,
    });
  });

  it("needs the parent to enable live mode or broaden its keys", () => {
    expect(approvalsNeeded(ACTIVE, withChanges({ enabled: true })).parent).toBe(true);
    expect(
      approvalsNeeded(ACTIVE, withChanges({ allowlist: [...ACTIVE.live_mode.allowlist, "x"] }))
        .parent,
    ).toBe(true);
  });

  it("explains the gate for the viewing role", () => {
    const none = { coach: false, parent: false };
    const coach = { coach: true, parent: false };
    const parent = { coach: false, parent: true };
    const both = { coach: true, parent: true };
    expect(approvalText(none, "parent")).toBeNull();
    expect(approvalText(both, "parent")).toContain("two versions");
    expect(approvalText(coach, "parent")).toContain("needs the coach");
    expect(approvalText(coach, "coach")).toBeNull();
    expect(approvalText(parent, "coach")).toContain("needs the parent");
    expect(approvalText(parent, "parent")).toBeNull();
  });
});
