/**
 * The forbidden state for the T4 operations screens: the viewing role may not
 * see this surface (US-L3). It is an alert, so it is announced and matches the
 * shared honest-states check, and it names which roles the screen serves.
 *
 * Local workaround until the design system has a forbidden primitive
 * (requested from T2 in the T4 status file).
 */

import { ShieldAlert } from "lucide-react";
import { rolesText, type Role } from "./access";

export interface ForbiddenProps {
  /** Roles the screen serves, e.g. ["parent", "coach"]. */
  roles: readonly Role[];
  /** The server's own words when it answered 403, shown verbatim. */
  detail?: string;
}

export function Forbidden({ roles, detail }: ForbiddenProps) {
  return (
    <div
      role="alert"
      data-testid="forbidden"
      className="flex flex-col gap-2 rounded-xl border-2 border-border bg-surface px-5 py-4"
    >
      <p className="flex items-center gap-2 text-lg font-semibold text-ink">
        <ShieldAlert aria-hidden="true" className="size-6 shrink-0 text-warning" />
        {`This screen is for the ${rolesText(roles)}.`}
      </p>
      <p className="text-ink-muted">Sign in with that role to see it.</p>
      {detail !== undefined && <p className="text-sm text-ink-muted">{detail}</p>}
    </div>
  );
}
