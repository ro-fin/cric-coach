/**
 * Sign-in journeys (US-L3 role access, T2's F2 login + proxy).
 * Every role signs in through the real form against the real API.
 */

import { expect, test, type Page } from "@playwright/test";
import { expectNoA11yViolations, expectSignedInAs, signIn, TOKENS, type Role } from "./support";

/**
 * The sign-in form's refusal. A production build also renders Next.js's hidden
 * route announcer with role="alert", so match the form's alert by its text.
 */
function refusal(page: Page) {
  return page.getByRole("alert").filter({ hasText: /token/i });
}

test("an unauthenticated visit is sent to sign in", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: "Sign in to cricAI" })).toBeVisible();
  await expectNoA11yViolations(page);
});

test("a wrong token is refused with a visible reason", async ({ page }) => {
  await page.goto("/login");
  await page.getByRole("radio", { name: /Player/ }).check();
  await page.getByLabel("Role token").fill("not-a-real-token");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(refusal(page)).toBeVisible();
  await expect(page).toHaveURL(/\/login/);
});

test("a token for another role is refused", async ({ page }) => {
  await page.goto("/login");
  await page.getByRole("radio", { name: /Coach/ }).check();
  await page.getByLabel("Role token").fill(TOKENS.player);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(refusal(page)).toBeVisible();
});

for (const role of ["player", "parent", "coach"] as const satisfies readonly Role[]) {
  test(`the ${role} signs in and the shell shows the role`, async ({ page }) => {
    await signIn(page, role);
    await expect(page).toHaveURL(/\/$/);
    await expectSignedInAs(page, role);
  });
}

test("a deep link survives sign-in", async ({ page }) => {
  await signIn(page, "parent", "/sessions");
  await expect(page).toHaveURL(/\/sessions$/);
});

test("the API token never reaches browser JavaScript", async ({ page }) => {
  await signIn(page, "player");
  const cookies = await page.context().cookies();
  const session = cookies.find((cookie) => cookie.name === "cricai_session");
  expect(session?.httpOnly).toBe(true);
  const visible = await page.evaluate(() => document.cookie + JSON.stringify(localStorage));
  expect(visible).not.toContain(TOKENS.player);
});
