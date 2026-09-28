/** US-K2: SVG pitch map — cells, overlays, frames, mirror, honest points. */

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { BouncePoint, HeatmapCell } from "./api";
import { cellRect, MARGIN, NO_DATA_FILL, SVG_WIDTH, viridis } from "./geometry";
import PitchMapSvg from "./PitchMapSvg";

const CELLS: HeatmapCell[] = [
  {
    line: "off",
    length: "good",
    balls: 6,
    control_pct: 60,
    false_shot_pct: 20,
    sources: { manual: 4, auto: 2 },
    hollow: 1,
  },
  {
    line: "middle",
    length: "full",
    balls: 3,
    control_pct: null,
    false_shot_pct: null,
    sources: { manual: 0, auto: 3 },
    hollow: 0,
  },
];

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
    ball_no: 3,
    line: "middle",
    length: "full",
    pitch_x: 3.0,
    pitch_y: 0.0,
    source: "auto",
    confidence: 0.9,
    hollow: false,
  },
  {
    ball_no: 9,
    line: "middle",
    length: "short",
    pitch_x: 21.5,
    pitch_y: 0.0,
    source: "auto",
    confidence: 0.8,
    hollow: false,
  },
];

function renderMap(overrides: Partial<Parameters<typeof PitchMapSvg>[0]> = {}) {
  const onSelectCell = vi.fn();
  const view = render(
    <PitchMapSvg
      cells={CELLS}
      points={POINTS}
      flaggedBalls={[3]}
      handedness="right"
      frame="batting_end"
      showBouncePoints
      colorByControl={false}
      targets={[]}
      showTargets={false}
      selectedCell={null}
      onSelectCell={onSelectCell}
      {...overrides}
    />,
  );
  return { onSelectCell, view };
}

describe("cells", () => {
  it("draws all 16 zone cells shaded by density with server counts", () => {
    renderMap();
    expect(screen.getAllByRole("button")).toHaveLength(16);
    expect(screen.getByTestId("cell-off-good")).toHaveAttribute("fill", viridis(1)); // peak cell
    expect(screen.getByTestId("cell-middle-full")).toHaveAttribute("fill", viridis(0.5));
    expect(screen.getByTestId("cell-leg-yorker")).toHaveAttribute("fill", viridis(0)); // no data
    expect(screen.getByTestId("count-off-good")).toHaveTextContent("6");
    expect(screen.getByTestId("count-leg-yorker")).toHaveTextContent("0");
  });

  it("shades by control % when toggled, neutral when control is unknowable", () => {
    renderMap({ colorByControl: true });
    expect(screen.getByTestId("cell-off-good")).toHaveAttribute("fill", viridis(0.6));
    expect(screen.getByTestId("cell-middle-full")).toHaveAttribute("fill", NO_DATA_FILL);
    expect(screen.getByTestId("cell-leg-yorker")).toHaveAttribute("fill", NO_DATA_FILL);
    expect(screen.getByTestId("count-middle-full")).toHaveAttribute("fill", "#000000");
    expect(screen.getByTestId("count-off-good")).toHaveAttribute("fill", "#000000"); // 60% > 0.5
  });

  it("flips count-label contrast against the density ramp", () => {
    renderMap();
    expect(screen.getByTestId("count-off-good")).toHaveAttribute("fill", "#000000"); // bright peak
    expect(screen.getByTestId("count-leg-yorker")).toHaveAttribute("fill", "#ffffff"); // dark zero
  });

  it("selects a cell by click and by keyboard", () => {
    const { onSelectCell } = renderMap();
    fireEvent.click(screen.getByTestId("cell-off-good"));
    expect(onSelectCell).toHaveBeenCalledWith({ line: "off", length: "good" });
    fireEvent.keyDown(screen.getByTestId("cell-middle-full"), { key: "Enter" });
    expect(onSelectCell).toHaveBeenCalledWith({ line: "middle", length: "full" });
    fireEvent.keyDown(screen.getByTestId("cell-middle-full"), { key: "a" });
    expect(onSelectCell).toHaveBeenCalledTimes(2);
  });

  it("outlines the selected cell only", () => {
    renderMap({ selectedCell: { line: "off", length: "good" } });
    expect(screen.getByTestId("cell-off-good")).toHaveAttribute("stroke", "#111827");
    expect(screen.getByTestId("cell-off-full")).toHaveAttribute("stroke", "#ffffff");
    expect(screen.getByTestId("cell-leg-good")).toHaveAttribute("stroke", "#ffffff");
  });
});

describe("bounce points", () => {
  it("renders provenance-honest points: solid manual, hollow low-confidence, flagged ring", () => {
    renderMap();
    expect(screen.getByTestId("point-1")).toHaveAttribute("fill", "#d62728");
    expect(screen.getByTestId("point-1")).toHaveAttribute("stroke", "#ffffff");
    expect(screen.getByTestId("point-2")).toHaveAttribute("fill", "none"); // hollow
    expect(screen.getByTestId("point-3")).toHaveAttribute("stroke", "#f59e0b"); // flagged
  });

  it("renders an off-pitch bounce at its true spot instead of clipping it (T7 lofted)", () => {
    renderMap();
    const lofted = screen.getByTestId("point-9");
    expect(Number.parseFloat(lofted.getAttribute("cy") as string)).toBeLessThan(MARGIN);
  });

  it("hides the scatter when the bounce-point overlay is off", () => {
    renderMap({ showBouncePoints: false });
    expect(screen.queryByTestId("point-1")).not.toBeInTheDocument();
  });
});

describe("target zones", () => {
  it("outlines declared targets when the overlay is on", () => {
    renderMap({
      showTargets: true,
      targets: [{ line: "middle", length: "good", description: "stump line, good length" }],
    });
    const target = screen.getByTestId("target-middle-good");
    expect(target).toHaveAttribute("stroke-dasharray", "6 3");
    expect(target).toHaveAttribute("fill", "none");
  });

  it("draws no target outlines when the overlay is off", () => {
    renderMap({
      targets: [{ line: "middle", length: "good", description: "stump line, good length" }],
    });
    expect(screen.queryByTestId("target-middle-good")).not.toBeInTheDocument();
  });
});

describe("frames and handedness", () => {
  it("labels the batting-end frame with the bowler end far", () => {
    renderMap();
    expect(
      screen.getByRole("img", { name: "pitch map, batting end view" }),
    ).toBeInTheDocument();
    expect(screen.getByText("bowler end (far)")).toBeInTheDocument();
    expect(screen.getByText("batter end (near)")).toBeInTheDocument();
  });

  it("flips the end labels in the bowling-end frame", () => {
    renderMap({ frame: "bowling_end" });
    expect(
      screen.getByRole("img", { name: "pitch map, bowling end view" }),
    ).toBeInTheDocument();
    expect(screen.getByText("batter end (far)")).toBeInTheDocument();
    expect(screen.getByText("bowler end (near)")).toBeInTheDocument();
  });

  it("puts the off side on the right for a right-hander at the batting end", () => {
    renderMap();
    expect(screen.getByTestId("off-side-label")).toHaveAttribute(
      "x",
      String(SVG_WIDTH - MARGIN / 4),
    );
  });

  it("mirrors cells and the off-side label for a left-hander (T7 LH guest)", () => {
    renderMap({ handedness: "left" });
    expect(screen.getByTestId("off-side-label")).toHaveAttribute("x", String(MARGIN / 4));
    const expected = cellRect("batting_end", "left", "off", "good");
    const rect = screen.getByTestId("cell-off-good");
    expect(Number.parseFloat(rect.getAttribute("x") as string)).toBeCloseTo(expected.u);
    const rhExpected = cellRect("batting_end", "right", "off", "good");
    expect(expected.u).not.toBeCloseTo(rhExpected.u); // genuinely mirrored
  });
});
