/**
 * Role access and honest load states shared by the T4 operations screens.
 *
 * The server is the authority on access (US-L3): a 403 always renders the
 * forbidden state. The client pre-check only avoids a pointless request when
 * the viewing role is already known to be outside the route's role list.
 */

import { ApiError } from "@/lib/api";

/** Mirror of T2's `Role` (lib/auth/role.tsx, contract 3.3). */
export type Role = "parent" | "coach" | "player";

/** Pre-check verdict: `unknown` means the role is not known yet (no session
 * cookie read yet), so the request goes ahead and the API decides. */
export type Access = "allowed" | "forbidden" | "unknown";

export function accessFor(role: Role | null, allowed: readonly Role[]): Access {
  if (role === null) {
    return "unknown";
  }
  return allowed.includes(role) ? "allowed" : "forbidden";
}

/** The four honest states of one data component, plus forbidden. */
export type Load<T> =
  | { kind: "loading" }
  | { kind: "ready"; data: T }
  | { kind: "error"; message: string }
  | { kind: "forbidden"; message: string };

export const LOADING: Load<never> = { kind: "loading" };

export function ready<T>(data: T): Load<T> {
  return { kind: "ready", data };
}

/** Map a thrown value to the state it deserves: 401/403 are access answers,
 * everything else is an error with a retry. */
export function failure(error: unknown): Load<never> {
  if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
    return { kind: "forbidden", message: error.message };
  }
  return { kind: "error", message: error instanceof Error ? error.message : String(error) };
}

/** Human line for a role list: "parent or coach". */
export function rolesText(roles: readonly Role[]): string {
  if (roles.length <= 1) {
    return roles.join("");
  }
  return `${roles.slice(0, -1).join(", ")} or ${roles[roles.length - 1]}`;
}
