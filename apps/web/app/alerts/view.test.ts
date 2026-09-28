import { describe, expect, it } from "vitest";
import { alert } from "@/test/fixtures.ops";
import { ALERT_ROLES, audienceText, detailRows, replaceAlert, severityTone } from "./view";

describe("alerts view model", () => {
  it("serves parents and coaches", () => {
    expect(ALERT_ROLES).toEqual(["parent", "coach"]);
  });

  it("maps known severities and keeps unknown ones neutral", () => {
    expect(severityTone("info")).toBe("info");
    expect(severityTone("warning")).toBe("warning");
    expect(severityTone("error")).toBe("danger");
    expect(severityTone("critical")).toBe("danger");
    expect(severityTone("notice")).toBe("neutral");
  });

  it("renders detail values verbatim, objects as JSON", () => {
    expect(detailRows({ camera_id: "C3", frames: 12, extra: { a: 1 } })).toEqual([
      ["camera_id", "C3"],
      ["frames", "12"],
      ["extra", '{"a":1}'],
    ]);
    expect(detailRows({ cameras: ["C2", "C3"], mixed: [1, { a: 1 }] })).toEqual([
      ["cameras", "C2, C3"],
      ["mixed", '[1,{"a":1}]'],
    ]);
  });

  it("replaces the acknowledged row in place", () => {
    const a = alert({ id: "a" });
    const b = alert({ id: "b" });
    const acked = { ...b, acknowledged: true };
    expect(replaceAlert([a, b], acked)).toEqual([a, acked]);
  });

  it("explains the routed inbox per role", () => {
    expect(audienceText("coach")).toContain("Developer");
    expect(audienceText("parent")).toContain("Parent");
    expect(audienceText(null)).toBe("Alerts routed to your role.");
  });
});
