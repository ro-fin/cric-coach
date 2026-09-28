/**
 * Render a component the way the app shell renders it (T1, phase-8-team.md §3.4).
 *
 * `renderWithShell(ui, { role, route })` sets the browser location to `route`
 * (components read `window.location.search`, e.g. the ?ball=N deep link) and
 * wraps `ui` in the same providers app/layout.tsx uses: T2's RoleProvider
 * (so `useRole()` answers `role`) and ToastProvider (so `useToast()` works).
 * The AppShell chrome itself is not rendered: screens are tested in isolation,
 * and the shell has its own tests.
 */

import { render } from "@testing-library/react";
import type { RenderOptions, RenderResult } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { ToastProvider } from "@/components/ui/Toast";
import { RoleProvider } from "@/lib/auth/role";
import type { Role } from "@/lib/auth/role";

export type { Role } from "@/lib/auth/role";

export interface ShellOptions {
  /** Viewing role; null renders signed out. Defaults to the admin role so every surface is visible. */
  role?: Role | null;
  /** Path plus query to set as the current location before rendering. */
  route?: string;
}

export function renderWithShell(
  ui: ReactElement,
  { role = "parent", route = "/" }: ShellOptions = {},
  options: Omit<RenderOptions, "wrapper"> = {},
): RenderResult {
  window.history.replaceState({}, "", route);
  function ShellWrapper({ children }: { children: ReactNode }) {
    return (
      <RoleProvider role={role}>
        <ToastProvider>{children}</ToastProvider>
      </RoleProvider>
    );
  }
  return render(ui, { ...options, wrapper: ShellWrapper });
}
