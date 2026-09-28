/**
 * Metric chip formatting (US-K1: per-ball metric summary chips).
 *
 * Renders the pinned {value, unit, confidence, reason-if-null} contract
 * verbatim — values come from ball_metrics rows; nothing is computed here
 * beyond string formatting (server-data-faithful, US-K4 parity rule).
 */

import type { MetricValue } from "@/lib/api";

/** Human label for one metric value; nulls surface their recorded reason. */
export function formatMetricValue(metric: MetricValue): string {
  const { value, unit } = metric;
  if (value === null) {
    return `n/a — ${metric.reason ?? "no reason recorded"}`;
  }
  if (typeof value === "boolean") {
    return value ? "yes" : "no";
  }
  if (typeof value === "number") {
    return withUnit(formatNumber(value), unit);
  }
  if (Array.isArray(value)) {
    return withUnit(value.map(formatNumber).join(", "), unit);
  }
  return withUnit(value, unit);
}

function formatNumber(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

function withUnit(text: string, unit: string): string {
  return unit === "" ? text : `${text} ${unit}`;
}

/** Confidence badge text for a metric chip. */
export function confidenceLabel(metric: MetricValue): string {
  return `conf ${metric.confidence.toFixed(2)}`;
}
