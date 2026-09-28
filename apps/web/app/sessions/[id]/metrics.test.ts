import { describe, expect, it } from "vitest";
import type { MetricValue } from "@/lib/api";
import { confidenceLabel, formatMetricValue } from "./metrics";

function metric(overrides: Partial<MetricValue>): MetricValue {
  return { value: 1, unit: "", confidence: 0.9, ...overrides };
}

describe("formatMetricValue", () => {
  it("surfaces the recorded reason for null values (no silent zeros)", () => {
    expect(formatMetricValue(metric({ value: null, reason: "occluded" }))).toBe(
      "n/a — occluded",
    );
    expect(formatMetricValue(metric({ value: null }))).toBe("n/a — no reason recorded");
    expect(formatMetricValue(metric({ value: null, reason: null }))).toBe(
      "n/a — no reason recorded",
    );
  });

  it("renders booleans as yes/no", () => {
    expect(formatMetricValue(metric({ value: true }))).toBe("yes");
    expect(formatMetricValue(metric({ value: false }))).toBe("no");
  });

  it("renders numbers with units, trimming integers", () => {
    expect(formatMetricValue(metric({ value: 42, unit: "cm" }))).toBe("42 cm");
    expect(formatMetricValue(metric({ value: 1.23456, unit: "m" }))).toBe("1.23 m");
    expect(formatMetricValue(metric({ value: 7 }))).toBe("7");
  });

  it("renders numeric lists joined", () => {
    expect(formatMetricValue(metric({ value: [1, 2.5], unit: "px" }))).toBe("1, 2.50 px");
  });

  it("renders strings verbatim with units", () => {
    expect(formatMetricValue(metric({ value: "braced", unit: "" }))).toBe("braced");
    expect(formatMetricValue(metric({ value: "short", unit: "zone" }))).toBe("short zone");
  });
});

describe("confidenceLabel", () => {
  it("formats the confidence to two decimals", () => {
    expect(confidenceLabel(metric({ confidence: 0.875 }))).toBe("conf 0.88");
  });
});
