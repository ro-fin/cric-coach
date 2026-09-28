import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ClipOut, EventOut, TagOut } from "@/lib/api";
import BallTimeline, { DEFAULT_VIEWPORT_PX, OVERSCAN, ROW_PX } from "./BallTimeline";
import { buildBallRows } from "./timeline";
import type { BallRow } from "./timeline";

function tag(ballNo: number): TagOut {
  return {
    ball_no: ballNo,
    block_no: 1,
    line: "off",
    length: "good",
    shot: "drive",
    footwork: "front",
    contact: "middle",
    outcome: "controlled_ground_shot",
    control: true,
    source: "manual",
    ground_truth_eligible: true,
    created_by: "parent",
    audits: [],
  };
}

function event(ballNo: number): EventOut {
  return {
    id: `e${ballNo}`,
    session_id: "s1",
    ball_no: ballNo,
    start_ms: ballNo * 10_000,
    release_ms: ballNo * 10_000 + 500,
    contact_ms: null,
    end_ms: ballNo * 10_000 + 6000,
    confidence: 0.9,
    source: "auto",
    detector_version: "v1",
    valid: true,
    created_at: "2026-07-09T10:00:00Z",
  };
}

function clip(ballNo: number, cameraId: string): ClipOut {
  return {
    id: `c${ballNo}-${cameraId}`,
    session_id: "s1",
    ball_no: ballNo,
    camera_id: cameraId,
    object_key: `clips/b${ballNo}_${cameraId}.mp4`,
    start_ms: ballNo * 10_000,
    end_ms: ballNo * 10_000 + 6000,
    status: "cut",
    error: null,
  };
}

function makeRows(count: number): BallRow[] {
  const ballNos = Array.from({ length: count }, (_, i) => i + 1);
  return buildBallRows(
    ballNos.map(event),
    ballNos.filter((n) => n % 2 === 0).map(tag),
    ballNos.filter((n) => n % 3 === 0).map((n) => clip(n, "C1")),
  );
}

const MAX_WINDOW_ROWS = Math.ceil(DEFAULT_VIEWPORT_PX / ROW_PX) + 2 * OVERSCAN;

describe("BallTimeline", () => {
  it("renders O(window) chips independent of total balls (US-K1 PT windowing invariant)", () => {
    // The deterministic invariant that guards the < 2 s budget: the DOM node
    // count is bounded by the viewport window and does NOT grow with the ball
    // count. A wall-clock assert here measured worker uptime, was flaky under
    // machine load, and passed on a full O(n) render — the structural asserts
    // below are the ones with detection power (review finding 31).
    const first = render(
      <BallTimeline rows={makeRows(500)} selectedBallNo={null} onSelect={() => undefined} />,
    );
    const chips500 = screen.getAllByRole("button").length;
    expect(chips500).toBeLessThanOrEqual(MAX_WINDOW_ROWS);
    expect(screen.getByText("Ball 1")).toBeInTheDocument();
    expect(screen.queryByText("Ball 400")).not.toBeInTheDocument();
    first.unmount();
    const startedMs = performance.now();
    render(<BallTimeline rows={makeRows(5000)} selectedBallNo={null} onSelect={() => undefined} />);
    const elapsedMs = performance.now() - startedMs;
    // 10x the balls, identical DOM window: O(window), not O(n).
    expect(screen.getAllByRole("button").length).toBe(chips500);
    expect(screen.queryByText("Ball 4000")).not.toBeInTheDocument();
    // Generous smoke signal only — machine load must never fail the gate.
    if (elapsedMs > 10_000) {
      console.warn(`BallTimeline 5000-ball render took ${elapsedMs.toFixed(0)}ms (smoke signal)`);
    }
  });

  it("windows the visible slice as the user scrolls", () => {
    render(<BallTimeline rows={makeRows(500)} selectedBallNo={null} onSelect={() => undefined} />);
    fireEvent.scroll(screen.getByTestId("timeline-scroll"), {
      target: { scrollTop: 100 * ROW_PX },
    });
    expect(screen.queryByText("Ball 1")).not.toBeInTheDocument();
    expect(screen.getByText(`Ball ${100 - OVERSCAN + 1}`)).toBeInTheDocument();
    expect(screen.getAllByRole("button").length).toBeLessThanOrEqual(MAX_WINDOW_ROWS);
  });

  it("shows tag, event and clip summary chips per ball (US-K1)", () => {
    render(<BallTimeline rows={makeRows(6)} selectedBallNo={2} onSelect={() => undefined} />);
    const ball2 = screen.getByText("Ball 2").closest("button") as HTMLButtonElement;
    expect(ball2).toHaveAttribute("aria-pressed", "true");
    expect(ball2).toHaveAttribute("data-tone", "good");
    expect(ball2).toHaveTextContent("off · good · drive · controlled_ground_shot · controlled");
    expect(ball2).toHaveTextContent("event auto · conf 0.90");
    expect(ball2).toHaveTextContent("no clips");
    const ball3 = screen.getByText("Ball 3").closest("button") as HTMLButtonElement;
    expect(ball3).toHaveAttribute("data-tone", "untagged");
    expect(ball3).toHaveTextContent("1 clips");
    expect(ball3).toHaveAttribute("aria-pressed", "false");
  });

  it("labels balls without an event", () => {
    const rows: BallRow[] = [{ ballNo: 9, tag: tag(9), event: null, clips: [] }];
    render(<BallTimeline rows={rows} selectedBallNo={null} onSelect={() => undefined} />);
    expect(screen.getByText("no event")).toBeInTheDocument();
  });

  it("clicking a chip selects that ball (click → cameras seek, US-K1 AC)", () => {
    const onSelect = vi.fn();
    render(<BallTimeline rows={makeRows(6)} selectedBallNo={null} onSelect={onSelect} />);
    fireEvent.click(screen.getByText("Ball 4").closest("button") as HTMLButtonElement);
    expect(onSelect).toHaveBeenCalledWith(4);
  });

  it("says so when no balls match the filters", () => {
    render(<BallTimeline rows={[]} selectedBallNo={null} onSelect={() => undefined} />);
    expect(screen.getByTestId("timeline-empty")).toBeInTheDocument();
  });

  it("scrolls the deep-linked ball into the rendered window (US-K2 ?ball=N)", () => {
    const { container } = render(
      <BallTimeline
        rows={makeRows(500)}
        selectedBallNo={300}
        onSelect={() => undefined}
        scrollToBallNo={300}
      />,
    );
    expect(screen.getByText("Ball 300")).toBeInTheDocument();
    expect(screen.queryByText("Ball 1")).not.toBeInTheDocument();
    const scroller = container.querySelector('[data-testid="timeline-scroll"]') as HTMLDivElement;
    expect(scroller.scrollTop).toBe(299 * ROW_PX);
  });

  it("deep-scrolls only once: later row changes do not yank the scroll back", () => {
    const { rerender } = render(
      <BallTimeline
        rows={makeRows(500)}
        selectedBallNo={300}
        onSelect={() => undefined}
        scrollToBallNo={300}
      />,
    );
    expect(screen.getByText("Ball 300")).toBeInTheDocument();
    rerender(
      <BallTimeline
        rows={makeRows(400)}
        selectedBallNo={300}
        onSelect={() => undefined}
        scrollToBallNo={300}
      />,
    );
    // still windowed around ball 300 — the one-shot scroll does not re-fire
    expect(screen.getByText("Ball 300")).toBeInTheDocument();
    expect(screen.queryByText("Ball 1")).not.toBeInTheDocument();
  });

  it("ignores a deep-link ball that is not in the rows", () => {
    render(
      <BallTimeline
        rows={makeRows(500)}
        selectedBallNo={null}
        onSelect={() => undefined}
        scrollToBallNo={9999}
      />,
    );
    expect(screen.getByText("Ball 1")).toBeInTheDocument();
  });

  it("survives a deep-link request when no rows match the filters", () => {
    render(
      <BallTimeline
        rows={[]}
        selectedBallNo={null}
        onSelect={() => undefined}
        scrollToBallNo={7}
      />,
    );
    expect(screen.getByTestId("timeline-empty")).toBeInTheDocument();
  });

  it("honors a custom viewport height", () => {
    render(
      <BallTimeline
        rows={makeRows(500)}
        selectedBallNo={null}
        onSelect={() => undefined}
        viewportPx={80}
      />,
    );
    expect(screen.getAllByRole("button").length).toBeLessThanOrEqual(2 + 2 * OVERSCAN);
  });
});
