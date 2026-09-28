/**
 * Alerts screen view model (US-L4). Pure helpers: severity tones, the detail
 * object as label/value rows, and merging an acknowledged row back in.
 */

import type { Role } from "../pipeline/access";
import type { Tone } from "../pipeline/view";
import type { AlertOut } from "./api";

export const ALERT_ROLES: readonly Role[] = ["parent", "coach"];

const SEVERITY_TONES: Readonly<Record<string, Tone>> = {
  info: "info",
  warning: "warning",
  error: "danger",
  critical: "danger",
};

/** Severity is free text on the wire; unknown values stay neutral. */
export function severityTone(severity: string): Tone {
  return SEVERITY_TONES[severity] ?? "neutral";
}

/** The detail object as rows, values rendered verbatim (objects as JSON). */
export function detailRows(detail: Record<string, unknown>): [string, string][] {
  return Object.entries(detail).map(([key, value]) => [
    key,
    typeof value === "string" ? value : JSON.stringify(value),
  ]);
}

/** Replace one row with the server's answer, keeping the served order. */
export function replaceAlert(alerts: readonly AlertOut[], updated: AlertOut): AlertOut[] {
  return alerts.map((row) => (row.id === updated.id ? updated : row));
}

/** Which inbox the role sees (routing is enforced on the server). */
export function audienceText(role: Role | null): string {
  if (role === "coach") {
    return "Developer alerts: pipeline, model drift and canary checks.";
  }
  if (role === "parent") {
    return "Parent alerts: device health and data honesty notices.";
  }
  return "Alerts routed to your role.";
}
