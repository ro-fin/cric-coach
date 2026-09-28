/** US-J5: coach review queue — gate-held drafts listed with kind/period/due
 * date, per-item publish (blocked outcomes surface the audited reasons list
 * verbatim) and the honest coach-token gate on a 401/403 refusal. */

import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PublishOut, ReviewQueueItemOut } from "@/lib/api";
import ReviewQueue from "./ReviewQueue";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, listReviewQueue: vi.fn(), publishReport: vi.fn() };
});

import { ApiError, listReviewQueue, publishReport } from "@/lib/api";

const listMock = vi.mocked(listReviewQueue);
const publishMock = vi.mocked(publishReport);

function item(overrides: Partial<ReviewQueueItemOut> = {}): ReviewQueueItemOut {
  return {
    id: "r1",
    player_id: "p1",
    session_id: "s1",
    kind: "daily",
    period_start: "2026-07-09",
    period_end: "2026-07-09",
    review_due_at: "2026-07-10T18:00:00Z",
    created_at: "2026-07-09T19:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  // resetAllMocks (not clearAllMocks) also drops queued mock*Once values so
  // one test's queue script can never bleed into the next.
  vi.resetAllMocks();
  listMock.mockResolvedValue([item()]);
});

describe("ReviewQueue", () => {
  it("lists held drafts with kind, period, due date and a report link", async () => {
    listMock.mockResolvedValue([
      item(),
      item({ id: "r2", kind: "weekly", period_start: "2026-07-03", review_due_at: null }),
    ]);
    render(<ReviewQueue />);
    expect(screen.getByText("Loading the review queue…")).toBeInTheDocument();
    const items = await screen.findAllByTestId("review-item");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("daily report · 2026-07-09 to 2026-07-09");
    expect(items[0]).toHaveTextContent("review due 2026-07-10T18:00:00Z");
    // A missing deadline is shown honestly as unknown, never invented.
    expect(items[1]).toHaveTextContent("weekly report · 2026-07-03 to 2026-07-09");
    expect(items[1]).toHaveTextContent("review due —");
    const links = screen.getAllByRole("link", { name: "View report" });
    expect(links[0]).toHaveAttribute("href", "/reports?report_id=r1");
    expect(links[1]).toHaveAttribute("href", "/reports?report_id=r2");
  });

  it("says so when the queue is clear", async () => {
    listMock.mockResolvedValue([]);
    render(<ReviewQueue />);
    expect(await screen.findByTestId("review-empty")).toHaveTextContent(
      "The review queue is clear",
    );
  });

  it.each([[401], [403]])(
    "shows the coach-token gate on an HTTP %i refusal (US-L3)",
    async (status) => {
      listMock.mockRejectedValue(new ApiError(status, "nope"));
      render(<ReviewQueue />);
      const gate = await screen.findByTestId("review-forbidden");
      expect(gate).toHaveTextContent("coach surface");
      expect(gate).toHaveTextContent(`HTTP ${status}`);
    },
  );

  it("surfaces a non-role server refusal as an error, not the token gate", async () => {
    listMock.mockRejectedValue(new ApiError(500, "boom"));
    render(<ReviewQueue />);
    expect(await screen.findByTestId("review-error")).toHaveTextContent(
      "The lab server refused the review queue (HTTP 500).",
    );
    expect(screen.queryByTestId("review-forbidden")).not.toBeInTheDocument();
  });

  it("surfaces a network failure honestly", async () => {
    listMock.mockRejectedValue(new Error("wire down"));
    render(<ReviewQueue />);
    expect(await screen.findByTestId("review-error")).toHaveTextContent(
      "Could not reach the lab server for the review queue.",
    );
  });

  it("publishes an item, confirms, re-fetches the queue and disables a second click", async () => {
    publishMock.mockResolvedValue({ status: "published", reasons: [] });
    render(<ReviewQueue />);
    await screen.findByTestId("review-item");
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));
    expect(await screen.findByTestId("publish-published")).toHaveTextContent(
      "Published — the report is live.",
    );
    expect(publishMock).toHaveBeenCalledWith("r1");
    // Server truth wins: every decided attempt re-fetches the queue.
    expect(listMock).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "Publish" })).toBeDisabled();
  });

  it("surfaces a blocked outcome with the audited reasons list (US-G3/H5)", async () => {
    const blocked: PublishOut = {
      status: "blocked",
      reasons: ["claims check failed: control_pct", "safety verdict is stale (US-H5)"],
    };
    publishMock.mockResolvedValue(blocked);
    render(<ReviewQueue />);
    await screen.findByTestId("review-item");
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));
    expect(await screen.findByTestId("publish-blocked")).toHaveTextContent(
      "the publish gate refused this report",
    );
    const reasons = screen.getByTestId("blocked-reasons").querySelectorAll("li");
    expect(reasons).toHaveLength(2);
    expect(reasons[0]).toHaveTextContent("claims check failed: control_pct");
    expect(reasons[1]).toHaveTextContent("safety verdict is stale (US-H5)");
    // Blocked is retryable: the server gate is authoritative and re-runnable,
    // so after the coach fixes the cause the same button can ask again.
    expect(screen.getByRole("button", { name: "Publish" })).toBeEnabled();
    expect(screen.queryByTestId("publish-published")).not.toBeInTheDocument();
    // The server still lists the report (mock returns it again), so it is a
    // live queue item, not a departed one.
    expect(screen.queryByTestId("review-departed")).not.toBeInTheDocument();
  });

  it("keeps a blocked report visible with its reasons when the server drops it", async () => {
    // The publish gate persists BLOCKED, so the re-fetched DRAFT queue no
    // longer lists the report — the coach must still SEE why it vanished.
    listMock.mockResolvedValueOnce([item()]).mockResolvedValueOnce([]);
    publishMock.mockResolvedValue({ status: "blocked", reasons: ["safety verdict is stale"] });
    render(<ReviewQueue />);
    await screen.findByTestId("review-item");
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));
    expect(await screen.findByTestId("review-departed")).toHaveTextContent(
      "No longer in the review queue",
    );
    expect(screen.getByTestId("review-item")).toHaveTextContent("daily report");
    expect(screen.getByTestId("blocked-reasons")).toHaveTextContent("safety verdict is stale");
    expect(screen.getByRole("button", { name: "Publish" })).toBeEnabled();
  });

  it("retries a departed blocked report without duplicating it, and can publish it", async () => {
    listMock.mockResolvedValueOnce([item()]).mockResolvedValue([]);
    publishMock.mockResolvedValueOnce({ status: "blocked", reasons: ["stale verdict"] });
    render(<ReviewQueue />);
    await screen.findByTestId("review-item");
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));
    await screen.findByTestId("review-departed");
    // Coach fixes the cause elsewhere, then retries from the departed item.
    publishMock.mockResolvedValueOnce({ status: "published", reasons: [] });
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));
    expect(await screen.findByTestId("publish-published")).toHaveTextContent(
      "Published — the report is live.",
    );
    expect(publishMock).toHaveBeenCalledTimes(2);
    expect(screen.getAllByTestId("review-item")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Publish" })).toBeDisabled();
  });

  it("keeps the last known queue when the post-publish re-fetch fails", async () => {
    listMock.mockResolvedValueOnce([item()]).mockRejectedValueOnce(new Error("wire down"));
    publishMock.mockResolvedValue({ status: "blocked", reasons: ["stale verdict"] });
    render(<ReviewQueue />);
    await screen.findByTestId("review-item");
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));
    expect(await screen.findByTestId("publish-blocked")).toBeInTheDocument();
    expect(screen.getByTestId("review-item")).toBeInTheDocument();
    expect(screen.queryByTestId("review-error")).not.toBeInTheDocument();
    expect(screen.queryByTestId("review-departed")).not.toBeInTheDocument();
  });

  it("disables the button while a publish is in flight and ignores extra clicks", async () => {
    let resolvePublish!: (outcome: PublishOut) => void;
    publishMock.mockImplementation(() => new Promise((r) => (resolvePublish = r)));
    render(<ReviewQueue />);
    await screen.findByTestId("review-item");
    const button = screen.getByRole("button", { name: "Publish" });
    fireEvent.click(button);
    expect(button).toBeDisabled();
    fireEvent.click(button);
    fireEvent.click(button);
    expect(publishMock).toHaveBeenCalledTimes(1);
    await act(async () => {
      resolvePublish({ status: "published", reasons: [] });
    });
    expect(await screen.findByTestId("publish-published")).toBeInTheDocument();
  });

  it("reports a publish transport failure and leaves the button armed", async () => {
    publishMock.mockRejectedValue(new Error("down"));
    render(<ReviewQueue />);
    await screen.findByTestId("review-item");
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));
    expect(await screen.findByTestId("publish-error")).toHaveTextContent(
      "Publish did not reach the lab server - the report is unchanged.",
    );
    expect(screen.getByRole("button", { name: "Publish" })).toBeEnabled();
    // A transport failure produced no decision, so there is nothing to re-fetch.
    expect(listMock).toHaveBeenCalledTimes(1);
  });

  it("names the HTTP status when the server refused the publish (US-K4 honesty)", async () => {
    publishMock.mockRejectedValue(new ApiError(500, "boom"));
    render(<ReviewQueue />);
    await screen.findByTestId("review-item");
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));
    expect(await screen.findByTestId("publish-error")).toHaveTextContent(
      "The lab server refused the publish (HTTP 500) - the report is unchanged.",
    );
    expect(screen.getByRole("button", { name: "Publish" })).toBeEnabled();
  });

  it("ignores results and failures that land after unmount", async () => {
    let resolve!: (items: ReviewQueueItemOut[]) => void;
    listMock.mockImplementation(() => new Promise((r) => (resolve = r)));
    const first = render(<ReviewQueue />);
    first.unmount();
    await act(async () => {
      resolve([item()]);
    });
    expect(screen.queryByTestId("review-item")).not.toBeInTheDocument();

    let reject!: (reason: unknown) => void;
    listMock.mockImplementation(() => new Promise((_, r) => (reject = r)));
    const second = render(<ReviewQueue />);
    second.unmount();
    await act(async () => {
      reject(new Error("late"));
    });
    expect(screen.queryByTestId("review-error")).not.toBeInTheDocument();
  });
});
