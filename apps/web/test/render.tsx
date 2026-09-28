/**
 * Render a component the way the app shell renders it (T1, phase-8-team.md §3.4).
 *
 * `renderWithShell(ui, { role, route })` sets the browser location to `route`
 * (components read `window.location.search`, e.g. the ?ball=N deep link) and
 * wraps `ui` in the shell providers.
 *
 * PROVIDER STATUS: T2's `RoleProvider` (lib/auth/role.tsx) and `ToastProvider`
 * (components/ui) are not on develop yet. Until they land this wrapper is a
 * pass-through and `role` is exposed on `document.documentElement.dataset.role`
 * for components that need it in tests. When F1 lands, T1 replaces
 * `ShellWrapper` with the real providers; callers do not change.
 */

import { render } from "@testing-library/react";
import type { RenderOptions, RenderResult } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";

export type Role = "parent" | "coach" | "player";

export interface ShellOptions {
  /** Viewing role; defaults to the admin role so every surface is visible. */
  role?: Role;
  /** Path plus query to set as the current location before rendering. */
  route?: string;
}

function ShellWrapper({ children }: { children: ReactNode }) {
  return <>{children}</>;
}

export function renderWithShell(
  ui: ReactElement,
  { role = "parent", route = "/" }: ShellOptions = {},
  options: Omit<RenderOptions, "wrapper"> = {},
): RenderResult {
  window.history.replaceState({}, "", route);
  document.documentElement.dataset.role = role;
  return render(ui, { ...options, wrapper: ShellWrapper });
}
