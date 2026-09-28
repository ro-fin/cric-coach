import { describe, expect, it } from "vitest";
import { metricLabel } from "./labels";

describe("metricLabel", () => {
  it("turns a unit suffix into the unit", () => {
    expect(metricLabel("control_pct")).toBe("Control (%)");
    expect(metricLabel("head_movement_cm")).toBe("Head movement (cm)");
    expect(metricLabel("bat_speed_kph")).toBe("Bat speed (km/h)");
  });

  it("keeps keys without a unit readable", () => {
    expect(metricLabel("exit_velo")).toBe("Exit velo");
    expect(metricLabel("pct")).toBe("Pct");
  });

  it("passes an empty or underscore-only key through", () => {
    expect(metricLabel("")).toBe("");
    expect(metricLabel("__")).toBe("__");
  });
});
