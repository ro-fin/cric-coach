import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { COPY } from "./copy";
import {
  CHART_HEIGHT,
  CHART_PAD,
  CHART_WIDTH,
  directionTone,
  placePoints,
  TrendChart,
} from "./TrendChart";
import type { TrendSeries } from "./types";

function trend(overrides: Partial<TrendSeries> = {}): TrendSeries {
  return {
    metric: "control_pct",
    zone_key: "all",
    direction: "improving",
    qualified: true,
    points: [
      { date: "2026-06-03", value: 0.5714, n: 31 },
      { date: "2026-06-10", value: 0.6123, n: 40 },
      { date: "2026-06-17", value: 0.7051, n: 44 },
    ],
    ...overrides,
  };
}

describe("placePoints", () => {
  it("spaces points evenly and scales values between min and max", () => {
    const placed = placePoints(trend().points);
    expect(placed.map(({ x }) => x)).toEqual([
      CHART_PAD,
      CHART_PAD + (CHART_WIDTH - 2 * CHART_PAD) / 2,
      CHART_WIDTH - CHART_PAD,
    ]);
    expect(placed[0].y).toBeCloseTo(CHART_HEIGHT - CHART_PAD); // the minimum
    expect(placed[2].y).toBeCloseTo(CHART_PAD); // the maximum
  });

  it("centres a flat series and a single point", () => {
    const flat = placePoints([
      { date: "2026-06-03", value: 0.5, n: 31 },
      { date: "2026-06-10", value: 0.5, n: 31 },
    ]);
    expect(flat.map(({ y }) => y)).toEqual([CHART_HEIGHT / 2, CHART_HEIGHT / 2]);
    const single = placePoints([{ date: "2026-06-03", value: 0.5, n: 31 }]);
    expect(single).toEqual([
      { x: CHART_PAD, y: CHART_HEIGHT / 2, point: { date: "2026-06-03", value: 0.5, n: 31 } },
    ]);
  });
});

describe("TrendChart", () => {
  it("renders the API values and n counts verbatim (US-K4 data parity)", () => {
    render(<TrendChart trend={trend()} />);
    for (const value of ["0.5714", "0.6123", "0.7051"]) {
      expect(screen.getByText(value)).toBeInTheDocument();
    }
    for (const n of ["n=31", "n=40", "n=44"]) {
      expect(screen.getByText(n)).toBeInTheDocument();
    }
    expect(screen.getByTestId("direction")).toHaveTextContent("improving");
  });

  it("shades confidence only for a qualified series", () => {
    render(<TrendChart trend={trend()} />);
    expect(screen.getByTestId("confidence-shade")).toBeInTheDocument();
    expect(screen.queryByTestId("unqualified-note")).not.toBeInTheDocument();
  });

  it("marks an unqualified series honestly: dashed line, visible note", () => {
    const { container } = render(<TrendChart trend={trend({ qualified: false })} />);
    expect(screen.queryByTestId("confidence-shade")).not.toBeInTheDocument();
    expect(screen.getByTestId("unqualified-note")).toHaveTextContent(COPY.unqualifiedNote);
    const line = container.querySelector("polyline");
    expect(line).toHaveAttribute("stroke-dasharray", "4 3");
  });

  it("never prints a direction verdict for an unqualified series (US-G5 guard)", () => {
    // Two 8-ball sessions must not read as "regressing" to a parent — the
    // >=3-session/>=30-ball qualification gate suppresses the verdict itself.
    render(
      <TrendChart
        trend={trend({
          qualified: false,
          direction: "regressing",
          points: [
            { date: "2026-06-03", value: 0.61, n: 8 },
            { date: "2026-06-10", value: 0.54, n: 8 },
          ],
        })}
      />,
    );
    expect(screen.getByTestId("direction")).toHaveTextContent(COPY.unqualifiedDirection);
    expect(screen.queryByText("regressing")).not.toBeInTheDocument();
    expect(screen.queryByText("improving")).not.toBeInTheDocument();
  });

  it("names the metric and zone so per-zone context is visible", () => {
    render(<TrendChart trend={trend({ zone_key: "off/good" })} />);
    expect(screen.getByLabelText("trend control_pct off/good")).toBeInTheDocument();
    expect(screen.getByText("(off/good)")).toBeInTheDocument();
  });

  it("renders nothing for a series without points", () => {
    const { container } = render(<TrendChart trend={trend({ points: [] })} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("directionTone", () => {
  it("colours served directions and keeps unqualified series neutral", () => {
    expect(directionTone("improving", true)).toBe("success");
    expect(directionTone("flat", true)).toBe("neutral");
    expect(directionTone("regressing", true)).toBe("warning");
    expect(directionTone("regressing", false)).toBe("neutral");
  });
});
