/**
 * Role journeys over the seeded demo lab, mirroring docs/uat_scripts.md.
 *
 * The demo (scripts/dev_stack.py) has one analyzed session with 24 tagged
 * balls, 12 of them edged cover drives full outside off; a degraded session
 * missing camera C2; a published daily report for 2026-09-26 whose one
 * correction names those 12 balls; and a 2026-09-27 draft held in the coach
 * review queue. Every journey starts from a fresh seed.
 */

import { expect } from "@playwright/test";
import { expectNoA11yViolations, signIn, test } from "./support";

const CORRECTION = /Get your front foot to the pitch of the ball/;

test.describe("player (UAT-P)", () => {
  test("UAT-P2: today's report shows one correction, one drill and one goal", async ({
    page,
    demo,
  }) => {
    expect(demo.published_report_id).toBeTruthy();
    await signIn(page, "player");
    await expect(page.getByRole("heading", { name: "Today's report" })).toBeVisible();
    await expect(page.getByText(CORRECTION)).toBeVisible();
    await expect(page.getByText("Report for 2026-09-26")).toBeVisible();
    await expect(page.getByText(/Front-foot ladder on full balls outside off/)).toBeVisible();
    await expect(page.getByRole("heading", { name: "Workload" })).toBeVisible();
    // The gate-held draft for 2026-09-27 is never shown to the player.
    await expect(page.getByText("Report for 2026-09-27")).toHaveCount(0);
    await expectNoA11yViolations(page);
  });

  test("UAT-P1: find a cover drive in today's session", async ({ page, demo }) => {
    await signIn(page, "player", "/sessions");
    await expect(page.getByRole("heading", { name: "Sessions" })).toBeVisible();
    await page.getByRole("link", { name: /2026-09-26/ }).click();
    await expect(page).toHaveURL(new RegExp(`/sessions/${demo.analyzed_session_id}`));
    const timeline = page.getByRole("list", { name: "ball timeline" });
    const ball = (n: number) => timeline.getByRole("button", { name: new RegExp(`^Ball ${n} `) });
    await expect(ball(1)).toBeVisible();

    // Script step 2: Shot = cover_drive leaves exactly the 12 seeded cover drives.
    // Anchored: each filter <label> wraps its <select>, so every accessible name
    // also contains the option texts (see T1 request to T3).
    await page.getByRole("combobox", { name: /^Shot/ }).selectOption("cover_drive");
    await expect(ball(1)).toHaveCount(0);
    await expect(timeline.getByRole("button", { name: /^Ball \d+ / })).toHaveCount(12);

    // Script step 3: pick one; it becomes the selected ball.
    await ball(4).click();
    await expect(ball(4)).toHaveAttribute("aria-pressed", "true");
    await expectNoA11yViolations(page);
  });

  test("the review queue is not the player's", async ({ page, demo }) => {
    expect(demo.draft_report_id).toBeTruthy();
    await signIn(page, "player");
    await expect(page.getByRole("link", { name: /Review/ }).filter({ visible: true })).toHaveCount(
      0,
    );
  });
});

test.describe("parent (UAT-PA)", () => {
  test("a degraded session says which camera is missing", async ({ page, demo }) => {
    expect(demo.degraded_session_id).toBeTruthy();
    await signIn(page, "parent", "/sessions");
    const degraded = page.getByRole("listitem").filter({ hasText: "2026-09-27" });
    await expect(degraded.getByText("degraded")).toBeVisible();
    await expect(degraded.getByText("Missing view: C2")).toBeVisible();
    const healthy = page.getByRole("listitem").filter({ hasText: "2026-09-26" });
    await expect(healthy.getByText("Missing view")).toHaveCount(0);
  });
});

test.describe("coach (UAT-C2)", () => {
  test("publishes the gate-held draft from the review queue", async ({ page, demo }) => {
    await signIn(page, "coach", "/review");
    const items = page.getByTestId("review-item");
    await expect(items).toHaveCount(1);
    await expect(items.getByRole("link", { name: "View report" })).toHaveAttribute(
      "href",
      `/reports?report_id=${demo.draft_report_id}`,
    );
    await items.getByRole("button", { name: "Publish" }).click();
    await expect(page.getByText("Published — the report is live.")).toBeVisible();
    await expect(items.getByRole("button", { name: "Publish" })).toBeDisabled();

    // The decision is real: after a reload the queue is empty.
    await page.reload();
    await expect(page.getByTestId("review-item")).toHaveCount(0);
  });
});
