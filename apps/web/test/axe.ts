/**
 * Accessibility gate for component tests (T1, phase-8-team.md §3.4).
 *
 * Runs axe-core (WCAG 2.x A/AA rule set) over a rendered container and fails
 * with a readable list of violations. Two rules are off by default:
 *   color-contrast — jsdom does not compute styles, so contrast is meaningless
 *                    here; the token palette is checked by T2's design tests.
 *   region         — components are rendered outside any page landmark.
 * Pass `rules` to change that for a full-page render.
 */

import axe from "axe-core";
import type { RunOptions } from "axe-core";
import { expect } from "vitest";

export interface AxeOptions {
  rules?: RunOptions["rules"];
}

const DEFAULT_RULES: RunOptions["rules"] = {
  "color-contrast": { enabled: false },
  region: { enabled: false },
};

export async function expectNoA11yViolations(
  container: Element,
  options: AxeOptions = {},
): Promise<void> {
  const results = await axe.run(container, {
    rules: { ...DEFAULT_RULES, ...options.rules },
  });
  const report = results.violations.map(
    (violation) =>
      `${violation.id}: ${violation.help} (${violation.nodes
        .map((node) => node.target.join(" "))
        .join(", ")})`,
  );
  // The ids go in the message: vitest prints only the message, not the diff, on rejects.
  expect(report, `axe violations: ${report.join(" | ")}`).toEqual([]);
}
