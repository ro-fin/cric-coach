import { ShieldAlert } from "lucide-react";
import type { Role } from "@/lib/auth/roles";

export interface ForbiddenStateProps {
  /** Roles the screen serves, e.g. ["parent", "coach"]. */
  roles: readonly Role[];
  /** The server's own words when it answered 403, shown verbatim. */
  detail?: string;
}

/** Human line for a role list: "parent", "parent or coach", "player, parent or coach". */
export function rolesText(roles: readonly Role[]): string {
  if (roles.length <= 1) {
    return roles.join("");
  }
  return `${roles.slice(0, -1).join(", ")} or ${roles[roles.length - 1]}`;
}

/**
 * The honest "not for your role" state (US-L3): an alert naming who the
 * screen serves, with the server's 403 reason when there is one.
 */
export function ForbiddenState({ roles, detail }: ForbiddenStateProps) {
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
