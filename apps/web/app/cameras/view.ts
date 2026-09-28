/**
 * Cameras screen view model (US-A1/I1/A4). Pure formatting of served values;
 * nothing is derived beyond display text.
 */

import type { Role } from "../pipeline/access";
import type { CameraOut, CameraRole, CheckResult, HealthCheckRecordOut } from "./api";

/** Route table: the camera screen is the parent's (registry writes are
 * parent-only on the server). */
export const CAMERA_ROLES_ALLOWED: readonly Role[] = ["parent"];

const ROLE_LABELS: Readonly<Record<CameraRole, string>> = {
  batting_side: "Batting side",
  bowling_side: "Bowling side",
  wrist: "Wrist",
  front_on: "Front on",
  other: "Other",
};

export function roleLabel(role: CameraRole | null): string {
  return role === null ? "No role" : ROLE_LABELS[role];
}

/** "x 1.5 m, y -4.2 m, z 1.6 m" in the served key order. */
export function offsetText(offset: Record<string, number>): string {
  return Object.entries(offset)
    .map(([axis, value]) => `${axis} ${value} m`)
    .join(", ");
}

/** The per-check list of a record, or null when the record carries none. */
export function checksOf(record: HealthCheckRecordOut): CheckResult[] | null {
  const checks = record.results.checks;
  return Array.isArray(checks) ? checks : null;
}

/** Active configs grouped by camera id, eras in served order. */
export function erasByCamera(cameras: readonly CameraOut[]): [string, CameraOut[]][] {
  const groups = new Map<string, CameraOut[]>();
  for (const camera of cameras) {
    const list = groups.get(camera.camera_id) ?? [];
    list.push(camera);
    groups.set(camera.camera_id, list);
  }
  return [...groups.entries()];
}
