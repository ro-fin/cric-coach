/**
 * Shared end-to-end helpers (T1). The tokens are the fixed development values
 * printed by scripts/dev_stack.py; they are never deployment secrets.
 */

import { join } from "node:path";
import { expect, type Page } from "@playwright/test";

export type Role = "parent" | "coach" | "player";

export const TOKENS: Record<Role, string> = {
  parent: "dev-parent-token",
  coach: "dev-coach-token",
  player: "dev-player-token",
};

export const LABELS: Record<Role, string> = {
  parent: "Parent",
  coach: "Coach",
  player: "Player",
};

/**
 * The shell renders the role chip twice (sidebar at >= 1024px, top bar below);
 * only one is visible per viewport, so match the visible one.
 */
export async function expectSignedInAs(page: Page, role: Role): Promise<void> {
  await expect(
    page.getByText(`Signed in as ${LABELS[role]}`).filter({ visible: true }),
  ).toHaveCount(1);
}

/** Sign in through the real /login form and wait until the app shell shows the role. */
export async function signIn(page: Page, role: Role, path = "/"): Promise<void> {
  await page.goto(path);
  await expect(page).toHaveURL(/\/login/);
  await page.getByRole("radio", { name: new RegExp(LABELS[role]) }).check();
  await page.getByLabel("Role token").fill(TOKENS[role]);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expectSignedInAs(page, role);
}

// Playwright compiles specs as CommonJS (no import.meta); it runs from apps/web,
// where axe-core is a direct devDependency.
const AXE_SCRIPT = join(process.cwd(), "node_modules", "axe-core", "axe.min.js");

/** Run axe-core (WCAG A/AA) on the current page and fail on any violation. */
export async function expectNoA11yViolations(page: Page): Promise<void> {
  await page.addScriptTag({ path: AXE_SCRIPT });
  const violations = await page.evaluate(async () => {
    const axe = (window as unknown as { axe: typeof import("axe-core") }).axe;
    const results = await axe.run(document, {
      runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] },
    });
    return results.violations.map(
      (violation) =>
        `${violation.id}: ${violation.help} (${violation.nodes
          .map((node) => node.target.join(" "))
          .join(", ")})`,
    );
  });
  expect(violations, `axe violations: ${violations.join(" | ")}`).toEqual([]);
}
