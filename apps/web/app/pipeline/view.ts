/**
 * Pipeline screen view model (US-J1/L1). Pure helpers only: badge tones,
 * query parsing and the role gates the router enforces. No metric is computed.
 */

import type { Role } from "./access";
import type { StageStatus } from "./api";

/** Route roles (route table 3.5) and the router's read gate agree. */
export const PIPELINE_ROLES: readonly Role[] = ["parent", "coach"];

export type Tone = "neutral" | "success" | "warning" | "danger" | "info";

const STATUS_TONES: Readonly<Record<StageStatus, Tone>> = {
  pending: "neutral",
  running: "info",
  succeeded: "success",
  failed: "danger",
  skipped: "warning",
};

/** Badge tone for a run or stage status; an unknown wire value stays neutral. */
export function statusTone(status: string): Tone {
  return (STATUS_TONES as Record<string, Tone>)[status] ?? "neutral";
}

/** Trigger, resume and re-derive are parent-only on the server. */
export function canTrigger(role: Role | null): boolean {
  return role === "parent";
}

export function sessionIdFromSearch(search: string): string | null {
  const value = new URLSearchParams(search).get("session");
  return value === null || value === "" ? null : value;
}

/** The API lists runs oldest first; the newest is the one to open first. */
export function newestRunId(runs: readonly { id: string }[]): string | null {
  return runs.length === 0 ? null : runs[runs.length - 1].id;
}

/** A served ISO timestamp in the viewer's locale; null means not finished.
 * Formatting only: the raw value stays in the <time dateTime> attribute. */
export function formatWhen(
  iso: string | null,
  pending = "not finished",
  style: "datetime" | "time" = "datetime",
): string {
  if (iso === null) {
    return pending;
  }
  const at = new Date(asUtc(iso));
  if (Number.isNaN(at.getTime())) {
    return iso;
  }
  return style === "time"
    ? at.toLocaleTimeString(undefined, { timeStyle: "medium" })
    : at.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/** The API stores UTC; some dialects (SQLite) serialise it without an offset.
 * A date-time with no zone designator is therefore read as UTC, not local. */
export function asUtc(iso: string): string {
  return /T\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(iso) ? `${iso}Z` : iso;
}

/** Short digest for a table cell; the full value stays in the title. */
export function shortDigest(digest: string | null): string {
  if (digest === null) {
    return "none";
  }
  return digest.length > 14 ? `${digest.slice(0, 14)}…` : digest;
}
