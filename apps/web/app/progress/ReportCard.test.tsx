import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ReportCard } from "./ReportCard";
import type { RollupReport } from "./types";

function weekly(overrides: Partial<RollupReport["body"]> = {}): RollupReport {
  return {
    id: "r1",
    kind: "weekly",
    status: "draft",
    period_start: "2026-06-15",
    period_end: "2026-06-21",
    body: {
      kind: "weekly",
      period: { start: "2026-06-15", end: "2026-06-21" },
      positive: "Another week of honest work in the bank - keep showing up.",
      honesty_banner: null,
      trends: [],
      milestones: [],
      ...overrides,
    },
  };
}

describe("ReportCard", () => {
  it("renders period, positive line and status verbatim", () => {
    render(<ReportCard report={weekly()} />);
    const card = screen.getByTestId("report-weekly");
    expect(card).toHaveAttribute("data-status", "draft");
    expect(screen.getByText("2026-06-15 to 2026-06-21")).toBeInTheDocument();
    expect(
      screen.getByText("Another week of honest work in the bank - keep showing up."),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("honesty-banner")).not.toBeInTheDocument();
    expect(screen.queryByTestId("body-milestones")).not.toBeInTheDocument();
  });

  it("shows the honesty banner and the period's milestones when present", () => {
    render(
      <ReportCard
        report={weekly({
          honesty_banner: "Not enough qualifying sessions for a trend claim yet.",
          milestones: [
            { kind: "streak", metric: "practice_days", value: 3, achieved_on: "2026-06-17" },
          ],
        })}
      />,
    );
    expect(screen.getByTestId("honesty-banner")).toHaveTextContent(
      "Not enough qualifying sessions",
    );
    expect(screen.getByTestId("body-milestones")).toHaveTextContent(
      "streak: practice_days 3 on 2026-06-17",
    );
  });
});
