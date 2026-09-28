/** US-K2 ST: cell -> ball list -> session-timeline links; honest no-bounce list (T7). */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { BouncePoint } from "./api";
import BallList from "./BallList";

const POINTS: BouncePoint[] = [
  {
    ball_no: 1,
    line: "off",
    length: "good",
    pitch_x: 6.5,
    pitch_y: 0.2,
    source: "manual",
    confidence: null,
    hollow: false,
  },
  {
    ball_no: 2,
    line: "off",
    length: "good",
    pitch_x: 7.0,
    pitch_y: 0.3,
    source: "auto",
    confidence: 0.4,
    hollow: true,
  },
  {
    ball_no: 4,
    line: "off",
    length: "full",
    pitch_x: 3.5,
    pitch_y: 0.2,
    source: "auto",
    confidence: 0.9,
    hollow: false,
  },
  {
    ball_no: 5,
    line: "leg",
    length: "good",
    pitch_x: 6.0,
    pitch_y: -0.5,
    source: "manual",
    confidence: null,
    hollow: false,
  },
];

describe("BallList", () => {
  it("prompts for a cell before listing balls", () => {
    render(
      <BallList
        sessionId="s-1"
        points={POINTS}
        flaggedBalls={[]}
        noBounceBalls={[]}
        selectedCell={null}
      />,
    );
    expect(screen.getByTestId("no-cell-selected")).toBeInTheDocument();
    expect(screen.queryByTestId("no-bounce-list")).not.toBeInTheDocument();
  });

  it("lists only the selected cell's balls, linking into the session timeline", () => {
    render(
      <BallList
        sessionId="s-1"
        points={POINTS}
        flaggedBalls={[2]}
        noBounceBalls={[]}
        selectedCell={{ line: "off", length: "good" }}
      />,
    );
    expect(screen.getByTestId("ball-link-1")).toHaveAttribute("href", "/sessions/s-1?ball=1");
    expect(screen.getByTestId("ball-link-2")).toHaveAttribute("href", "/sessions/s-1?ball=2");
    expect(screen.queryByTestId("ball-link-4")).not.toBeInTheDocument(); // length differs
    expect(screen.queryByTestId("ball-link-5")).not.toBeInTheDocument(); // line differs
    expect(screen.getByTestId("ball-link-1").parentElement).toHaveTextContent(
      "ball 1 — manual",
    );
    expect(screen.getByTestId("ball-link-2").parentElement).toHaveTextContent(
      "ball 2 — auto, low confidence, flagged for review",
    );
  });

  it("says so when the selected cell holds no balls", () => {
    render(
      <BallList
        sessionId="s-1"
        points={POINTS}
        flaggedBalls={[]}
        noBounceBalls={[]}
        selectedCell={{ line: "middle", length: "yorker" }}
      />,
    );
    expect(screen.getByTestId("cell-empty")).toBeInTheDocument();
  });

  it("lists balls without a bounce point honestly (T7 beamer / full toss)", () => {
    render(
      <BallList
        sessionId="s-1"
        points={POINTS}
        flaggedBalls={[]}
        noBounceBalls={[7]}
        selectedCell={null}
      />,
    );
    expect(screen.getByTestId("no-bounce-list")).toHaveTextContent(
      "1 ball without a bounce point",
    );
    expect(screen.getByTestId("no-bounce-link-7")).toHaveAttribute(
      "href",
      "/sessions/s-1?ball=7",
    );
  });

  it("pluralizes the no-bounce headline", () => {
    render(
      <BallList
        sessionId="s-1"
        points={POINTS}
        flaggedBalls={[]}
        noBounceBalls={[7, 8]}
        selectedCell={null}
      />,
    );
    expect(screen.getByTestId("no-bounce-list")).toHaveTextContent(
      "2 balls without a bounce point",
    );
  });
});
