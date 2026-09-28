import { describe, expect, it } from "vitest";
import { camera, healthCheck } from "@/test/fixtures.ops";
import { CAMERA_ROLES_ALLOWED, checksOf, erasByCamera, offsetText, roleLabel } from "./view";

describe("cameras view model", () => {
  it("is a parent screen", () => {
    expect(CAMERA_ROLES_ALLOWED).toEqual(["parent"]);
  });

  it("labels every role and the empty role", () => {
    expect(roleLabel("batting_side")).toBe("Batting side");
    expect(roleLabel("bowling_side")).toBe("Bowling side");
    expect(roleLabel("wrist")).toBe("Wrist");
    expect(roleLabel("front_on")).toBe("Front on");
    expect(roleLabel("other")).toBe("Other");
    expect(roleLabel(null)).toBe("No role");
  });

  it("prints offsets verbatim in served order", () => {
    expect(offsetText({ x: 1.5, y: -4.2, z: 1.6 })).toBe("x 1.5 m, y -4.2 m, z 1.6 m");
  });

  it("reads per-check results, or null when absent", () => {
    expect(checksOf(healthCheck())).toHaveLength(2);
    expect(checksOf(healthCheck({ results: {} }))).toBeNull();
  });

  it("groups eras by camera id", () => {
    const c1a = camera("C1", { era_no: 1, active: false });
    const c1b = camera("C1", { era_no: 2 });
    const c2 = camera("C2");
    expect(erasByCamera([c1a, c1b, c2])).toEqual([
      ["C1", [c1a, c1b]],
      ["C2", [c2]],
    ]);
  });
});
