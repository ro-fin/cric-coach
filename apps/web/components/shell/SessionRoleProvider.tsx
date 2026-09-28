import { cookies } from "next/headers";
import type { ReactNode } from "react";
import { RoleProvider } from "@/lib/auth/role";
import { decodeSession, SESSION_COOKIE } from "@/lib/auth/session";

/**
 * Server component: reads the signed-in role from the httpOnly session
 * cookie and provides it to the client tree. Only the role crosses to the
 * browser; the token in the same cookie never does.
 */
export async function SessionRoleProvider({ children }: { children: ReactNode }) {
  const session = decodeSession((await cookies()).get(SESSION_COOKIE)?.value);
  return <RoleProvider role={session?.role ?? null}>{children}</RoleProvider>;
}
