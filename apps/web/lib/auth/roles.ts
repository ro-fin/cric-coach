/**
 * Viewing roles (US-L3). Plain module (no "use client") so server code — the
 * layout, route handlers, middleware — can share the vocabulary.
 */
export type Role = "parent" | "coach" | "player";

export const ROLES: readonly Role[] = ["player", "parent", "coach"];

export const ROLE_LABELS: Record<Role, string> = {
  player: "Player",
  parent: "Parent",
  coach: "Coach",
};

export function isRole(value: unknown): value is Role {
  return typeof value === "string" && (ROLES as readonly string[]).includes(value);
}
