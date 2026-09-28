/**
 * Analytics journeys over T4's screens, mirroring docs/uat_scripts.md UAT-P4
 * (pitch map) and UAT-PA3 (the parent's dashboard questions).
 *
 * The demo lab has tags for every ball but no bounce points, so the pitch map
 * must list all 24 balls AND say honestly that zone counts need bounce points.
 * The player has bowled nothing, so the workload panel must show the safe,
 * empty truth rather than invent numbers.
 */

import { expect } from "@playwright/test";
import { expectNoA11yViolations, signIn, test } from "./support";

test.describe("player (UAT-P4)", () => {
  test("the pitch map lists every ball and is honest about missing bounce points", async ({
    page,
    demo,
  }) => {
    await signIn(page, "player", `/pitchmap/${demo.analyzed_session_id}`);
    await expect(page.getByRole("heading", { name: "Pitch map" })).toBeVisible();
    await expect(page.getByText("No bounce points on the map yet")).toBeVisible();
    await expect(page.getByText(/Zone counts appear once balls have bounce points/)).toBeVisible();
    // Tagged balls without a bounce point are still listed, each one a deep link
    // into the session detail (US-K2 ?ball=N contract): the honest full ball list.
    await expect(page.getByTestId(/^no-bounce-link-/)).toHaveCount(24);
    await expect(page.getByTestId("no-bounce-link-1")).toHaveAttribute(
      "href",
      `/sessions/${demo.analyzed_session_id}?ball=1`,
    );
    await expectNoA11yViolations(page);
  });
});

test.describe("parent (UAT-PA3)", () => {
  test("the progress dashboard answers the safety questions from API data only", async ({
    page,
    demo,
  }) => {
    await signIn(page, "parent", `/progress?player=${demo.player_id}`);
    await expect(page.getByRole("heading", { name: "Progress" })).toBeVisible();
    // Q1 safe to bowl: the workload panel shows the API's overs, ceiling and remaining balls.
    await expect(page.getByRole("heading", { name: "Workload safety" })).toBeVisible();
    for (const tile of ["Overs this rolling week", "Allowed overs", "Balls left this week"]) {
      await expect(page.getByText(tile)).toBeVisible();
    }
    await expect(page.getByText(/Bowling days:/)).toBeVisible();
    await expect(page.getByText("No workload flags.")).toBeVisible();
    // Q2 wellness flags: none, said plainly.
    await expect(page.getByText("No wellness flags.")).toBeVisible();
    // Q3 and Q4: no rollups yet, so trends and reports say so instead of inventing.
    await expect(
      page.getByText("No weekly report yet - trends appear after the first rollup."),
    ).toBeVisible();
    await expect(page.getByText("No rollup reports yet.")).toBeVisible();
    await expectNoA11yViolations(page);
  });

  test("progress without a player says how to pick one", async ({ page }) => {
    await signIn(page, "parent", "/progress");
    await expect(page.getByTestId("missing-player")).toBeVisible();
  });
});
