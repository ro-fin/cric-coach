/**
 * Interim axe check for the design-system tests, until T1's test/axe.ts
 * (expectNoA11yViolations) lands on develop. jsdom cannot compute colours, so
 * contrast is checked against the token palette instead; "region" is off
 * because primitives are rendered outside any page landmark.
 */
import axe from "axe-core";
import { expect } from "vitest";

export async function expectAxeClean(container: Element): Promise<void> {
  const results = await axe.run(container, {
    rules: { "color-contrast": { enabled: false }, region: { enabled: false } },
  });
  expect(results.violations.map((violation) => violation.id)).toEqual([]);
}
