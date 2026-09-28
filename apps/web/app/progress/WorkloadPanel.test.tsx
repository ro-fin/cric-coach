import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { expectAxeClean } from "@/lib/testing/axe";
import { COPY } from "./copy";
import type { WorkloadWindow } from "./types";
import { WorkloadPanel } from "./WorkloadPanel";

function window(overrides: Partial<WorkloadWindow> = {}): WorkloadWindow {
  return {
    window_start: "2026-06-11",
    window_end: "2026-06-17",
    weighted_balls: 81,
    weighted_overs: 13.5,
    bowling_days: ["2026-06-15", "2026-06-16"],
    ceiling_overs: 16,
    violations: [],
    remaining_balls: 15,
    ...overrides,
  };
}

const wellnessOk = { pain_active: false };

describe("WorkloadPanel", () => {
  it("is sticky so it can never scroll below the fold (US-K4)", () => {
    render(<WorkloadPanel window={window()} wellness={wellnessOk} />);
    expect(screen.getByTestId("workload-panel")).toHaveClass("sticky");
  });

  it("renders the API's overs, ceiling, remaining and days verbatim", () => {
    render(<WorkloadPanel window={window()} wellness={wellnessOk} />);
    expect(screen.getByTestId("overs")).toHaveTextContent(`${COPY.oversTile}13.5${COPY.oversUnit}`);
    expect(screen.getByTestId("ceiling")).toHaveTextContent(`${COPY.ceilingTile}16${COPY.oversUnit}`);
    expect(screen.getByTestId("remaining")).toHaveTextContent(
      `${COPY.remainingTile}15${COPY.ballsUnit}`,
    );
    expect(screen.getByTestId("bowling-days")).toHaveTextContent(
      `${COPY.bowlingDaysLabel}: 2026-06-15, 2026-06-16`,
    );
    expect(screen.getByTestId("no-violations")).toHaveTextContent(COPY.noWorkloadFlags);
    expect(screen.getByTestId("wellness")).toHaveTextContent(COPY.wellnessOk);
  });

  it("says why the ceiling and countdown are missing instead of showing a dash", () => {
    render(
      <WorkloadPanel
        window={window({ ceiling_overs: null, remaining_balls: null })}
        wellness={wellnessOk}
      />,
    );
    expect(screen.getByTestId("ceiling")).toHaveTextContent(COPY.noCeiling);
    expect(screen.getByTestId("remaining")).toHaveTextContent(COPY.noRemaining);
    expect(screen.getByTestId("workload-panel")).not.toHaveTextContent("—");
  });

  it("lists violations and the active pain flag", () => {
    render(
      <WorkloadPanel
        window={window({ violations: ["workload_ceiling", "day_pattern_violation"] })}
        wellness={{ pain_active: true }}
      />,
    );
    const flags = screen.getByTestId("violations");
    expect(flags).toHaveTextContent("workload_ceiling");
    expect(flags).toHaveTextContent("day_pattern_violation");
    expect(screen.getByTestId("wellness")).toHaveTextContent(COPY.wellnessFlag);
  });

  it("shows the empty-day pattern and the no-data state", () => {
    render(<WorkloadPanel window={window({ bowling_days: [] })} wellness={wellnessOk} />);
    expect(screen.getByTestId("bowling-days")).toHaveTextContent(COPY.noBowlingDays);
  });

  it("stays visible with an honest note when no summary exists yet", () => {
    render(<WorkloadPanel window={null} wellness={wellnessOk} />);
    expect(screen.getByTestId("workload-empty")).toHaveTextContent(COPY.noWorkloadData);
    expect(screen.getByTestId("wellness")).toBeInTheDocument();
  });

  it("is a named region with no axe violations", async () => {
    const { container } = render(<WorkloadPanel window={window()} wellness={wellnessOk} />);
    expect(screen.getByRole("region", { name: COPY.workloadTitle })).toBeInTheDocument();
    await expectAxeClean(container);
  });
});
