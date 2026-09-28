import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { expectNoA11yViolations } from "@/test/axe";
import { createFakeApi } from "@/test/fakeApi";
import type { FakeApi } from "@/test/fakeApi";
import { clip, event, phaseMetrics, session, tag } from "@/test/fixtures";
import { expectHonestStatesWith } from "@/test/states";
import SessionDetailClient from "./SessionDetailClient";

function seeded(): FakeApi {
  return createFakeApi({
    sessions: [session({ id: "s1" })],
    events: { s1: [event(1), event(2)] },
    tags: { s1: [tag(1), tag(2)] },
    clips: { s1: [clip(1, "C1"), clip(1, "C2")] },
  });
}

function renderDetail(api: FakeApi) {
  return render(<SessionDetailClient sessionId="s1" client={api} mediaBase="http://nas/m" />);
}

describe("SessionDetailClient honest states (Phase 8)", () => {
  it("shows loading, empty, error and forbidden for the session read", async () => {
    const api = seeded();
    await expectHonestStatesWith({
      render: () => renderDetail(api),
      driver: {
        reset: () => api.reset(),
        hold: () => api.hold("getSession"),
        respondEmpty: () => {
          api.respondWith("listEvents", []);
          api.respondWith("listTags", []);
          api.respondWith("listClips", []);
        },
        fail: (status) => api.failWith("getSession", status),
      },
      emptyText: "No balls recorded for this session yet.",
      forbiddenText: "Your role cannot see this session.",
    });
  });

  it("names a missing session and retries a failed load", async () => {
    const api = seeded();
    api.failWith("getSession", 404);
    const user = userEvent.setup();
    renderDetail(api);
    expect(await screen.findByText("This session does not exist.")).toBeInTheDocument();
    api.reset();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { name: "Ball 1" })).toBeInTheDocument();
  });

  it("shows a degraded capture's missing views verbatim", async () => {
    const api = createFakeApi({
      sessions: [session({ id: "s1", degraded: true, missing_views: ["C2"] })],
      events: { s1: [event(1)] },
    });
    renderDetail(api);
    const banner = await screen.findByRole("region", { name: "Incomplete data" });
    expect(banner).toHaveTextContent("Missing view: C2");
  });

  it("leads with the selected ball's player and keeps the timeline in its own card", async () => {
    renderDetail(seeded());
    const ball = await screen.findByRole("region", { name: "Ball 1" });
    const balls = screen.getByRole("region", { name: "Balls" });
    // video first in reading order: the player card precedes the timeline card
    expect(ball.compareDocumentPosition(balls) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(ball).getByTestId("player-video")).toBeInTheDocument();
    expect(within(balls).getByRole("list", { name: "ball timeline" })).toBeInTheDocument();
  });

  it("retries a failed metrics read and flags synthesized phases", async () => {
    const api = seeded();
    api.failWith("ballMetrics", 500);
    const user = userEvent.setup();
    renderDetail(api);
    const failure = await screen.findByTestId("metrics-error");
    expect(failure).toHaveTextContent("Metrics unavailable:");
    api.reset();
    api.respondWith("ballMetrics", [phaseMetrics("flight", {}, { stored: false })]);
    await user.click(within(failure).getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("not from the vision pipeline")).toBeInTheDocument();
  });

  it("has no axe violations", async () => {
    const { container } = renderDetail(seeded());
    await screen.findByRole("heading", { name: "Ball 1" });
    await screen.findByTestId("metrics-empty");
    await expectNoA11yViolations(container);
  });
});
