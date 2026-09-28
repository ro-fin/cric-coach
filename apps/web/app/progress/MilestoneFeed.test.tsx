import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { COPY } from "./copy";
import { kidLine, MilestoneFeed } from "./MilestoneFeed";
import type { Milestone } from "./types";

const personalBest: Milestone = {
  id: "m1",
  kind: "personal_best",
  metric: "control_pct",
  value: 0.7051,
  context: { n: 44 },
  achieved_on: "2026-06-17",
};

const volume: Milestone = {
  id: "m2",
  kind: "volume",
  metric: "bowling_balls",
  value: 100,
  context: { landmarks: [100] },
  achieved_on: "2026-06-08",
};

const streak: Milestone = {
  id: "m3",
  kind: "streak",
  metric: "practice_days",
  value: 3,
  context: { streak_start: "2026-06-15" },
  achieved_on: "2026-06-17",
};

describe("kidLine", () => {
  it("celebrates each kind age-appropriately", () => {
    expect(kidLine(personalBest)).toBe(COPY.kidPersonalBest);
    expect(kidLine(volume)).toBe(COPY.kidVolume);
    expect(kidLine(streak)).toBe(`3 ${COPY.kidStreakSuffix}`);
  });
});

describe("MilestoneFeed", () => {
  it("shows the empty-state encouragement when there are no milestones", () => {
    render(<MilestoneFeed milestones={[]} kidMode={false} />);
    expect(screen.getByTestId("milestones-empty")).toHaveTextContent(COPY.emptyMilestones);
  });

  it("parent view renders metric, value and date verbatim (data parity)", () => {
    render(<MilestoneFeed milestones={[personalBest, volume, streak]} kidMode={false} />);
    const items = screen.getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("control_pct 0.7051 on 2026-06-17");
    expect(items[1]).toHaveTextContent("bowling_balls 100 on 2026-06-08");
    expect(items[2]).toHaveTextContent("practice_days 3 on 2026-06-17");
    expect(items[0]).toHaveTextContent(COPY.milestoneLabels.personal_best);
  });

  it("kid mode swaps in celebration copy and drops the volume number", () => {
    render(<MilestoneFeed milestones={[personalBest, volume, streak]} kidMode={true} />);
    expect(screen.getByText(COPY.kidPersonalBest)).toBeInTheDocument();
    expect(screen.getByText(COPY.kidVolume)).toBeInTheDocument();
    expect(screen.getByText(`3 ${COPY.kidStreakSuffix}`)).toBeInTheDocument();
    expect(screen.queryByText(/100/)).not.toBeInTheDocument(); // no volume number
    expect(screen.queryByText(/0\.7051/)).not.toBeInTheDocument();
  });
});
