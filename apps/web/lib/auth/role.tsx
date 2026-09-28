"use client";

import { createContext, useContext, type ReactNode } from "react";
import type { Role } from "./roles";

export type { Role } from "./roles";

const RoleContext = createContext<Role | null>(null);

export interface RoleProviderProps {
  /** The signed-in role (read server-side from the session cookie); null when signed out. */
  role: Role | null;
  children: ReactNode;
}

export function RoleProvider({ role, children }: RoleProviderProps) {
  return <RoleContext.Provider value={role}>{children}</RoleContext.Provider>;
}

/** The current viewing role, or null when nobody is signed in. */
export function useRole(): Role | null {
  return useContext(RoleContext);
}
