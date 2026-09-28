import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { expectAxeClean } from "@/lib/testing/axe";
import { COPY } from "./copy";
import ProgressPage from "./page";
import type { Milestone, RollupReport, TrendSeries, WorkloadWindow } from "./types";
import { playerIdFromSearch, regressingTrends } from "./view";

const improving: TrendSeries = {
  metric: "control_pct",
  zone_key: "all",
  direction: "improving",
  qualified: true,
  points: [
    { date: "2026-06-03", value: 0.5714, n: 31 },
    { date: "2026-06-10", value: 0.6123, n: 40 },
    { date: "2026-06-17", value: 0.7051, n: 44 },
  ],
};

const regressing: TrendSeries = {
  metric: "head_stability_score",
  zone_key: "off/good",
  direction: "regressing",
  qualified: true,
  points: [
    { date: "2026-06-03", value: 0.82, n: 33 },
    { date: "2026-06-10", value: 0.66, n: 35 },
    { date: "2026-06-17", value: 0.49, n: 38 },
  ],
};

const unqualified: TrendSeries = {
  metric: "front_foot_direction_cm",
  zone_key: "all",
  direction: "regressing",
  qualified: false,
  points: [{ date: "2026-06-17", value: 12.25, n: 8 }],
};

const weekly: RollupReport = {
  id: "rw",
  kind: "weekly",
  status: "draft",
  period_start: "2026-06-15",
  period_end: "2026-06-21",
  body: {
    kind: "weekly",
    period: { start: "2026-06-15", end: "2026-06-21" },
    positive: "Another week of honest work in the bank - keep showing up.",
    honesty_banner: null,
    trends: [improving, regressing, unqualified],
    milestones: [
      { kind: "streak", metric: "practice_days", value: 3, achieved_on: "2026-06-17" },
    ],
  },
};

const monthly: RollupReport = {
  ...weekly,
  id: "rm",
  kind: "monthly",
  period_start: "2026-06-01",
  period_end: "2026-06-30",
  body: { ...weekly.body, kind: "monthly", period: { start: "2026-06-01", end: "2026-06-30" } },
};

const milestones: Milestone[] = [
  {
    id: "m1",
    kind: "personal_best",
    metric: "control_pct",
    value: 0.7051,
    context: { n: 44 },
    achieved_on: "2026-06-17",
  },
  {
    id: "m2",
    kind: "streak",
    metric: "practice_days",
    value: 3,
    context: { streak_start: "2026-06-15" },
    achieved_on: "2026-06-17",
  },
];

const workload: WorkloadWindow = {
  window_start: "2026-06-11",
  window_end: "2026-06-17",
  weighted_balls: 81,
  weighted_overs: 13.5,
  bowling_days: ["2026-06-15", "2026-06-16"],
  ceiling_overs: 16,
  violations: [],
  remaining_balls: 15,
};

function stubApi({ empty = false, painActive = false } = {}): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const payload = (() => {
        if (url.includes("kind=weekly")) return empty ? [] : [weekly];
        if (url.includes("kind=monthly")) return empty ? [] : [monthly];
        if (url.includes("/milestones/")) return empty ? [] : milestones;
        if (url.includes("/workload/")) return empty ? [] : [workload];
        return { pain_active: painActive };
      })();
      return { ok: true, status: 200, json: async () => payload };
    }),
  );
}

function visit(search: string): void {
  window.history.replaceState({}, "", `/progress${search}`);
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/progress");
});

describe("helpers", () => {
  it("reads the player id from the query string", () => {
    expect(playerIdFromSearch("?player=p1")).toBe("p1");
    expect(playerIdFromSearch("")).toBeNull();
  });

  it("selects only qualified regressing trends for the coaching frame", () => {
    expect(regressingTrends([improving, regressing, unqualified])).toEqual([regressing]);
  });
});

describe("ProgressPage states", () => {
  it("asks for a player id when the query param is missing", async () => {
    visit("");
    stubApi();
    render(<ProgressPage />);
    expect(await screen.findByTestId("missing-player")).toHaveTextContent(COPY.missingPlayer);
  });

  it("shows the failure note when the API is unreachable", async () => {
    visit("?player=p1");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("connection refused");
      }),
    );
    render(<ProgressPage />);
    expect(await screen.findByTestId("load-failed")).toHaveTextContent(COPY.loadFailed);
  });

  it("stays on the loading state while the dashboard is in flight", () => {
    visit("?player=p1");
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise(() => undefined)),
    );
    render(<ProgressPage />);
    expect(screen.getByText(COPY.loading)).toBeInTheDocument();
  });

  it("handles a lab with no rollups yet", async () => {
    visit("?player=p1");
    stubApi({ empty: true });
    render(<ProgressPage />);
    expect(await screen.findByTestId("no-trends")).toHaveTextContent(COPY.noTrends);
    expect(screen.getByTestId("no-reports")).toHaveTextContent(COPY.noReports);
    expect(screen.getByTestId("workload-empty")).toBeInTheDocument();
    expect(screen.getByTestId("milestones-empty")).toBeInTheDocument();
  });
});

describe("ProgressPage parent view (US-K4)", () => {
  it("renders EXACTLY the API numbers (data-parity acceptance)", async () => {
    visit("?player=p1");
    stubApi();
    render(<ProgressPage />);
    await screen.findByTestId("parent-view");

    // Trend values and per-point n, verbatim from the weekly body.
    for (const value of ["0.5714", "0.6123", "0.7051", "0.82", "0.66", "0.49", "12.25"]) {
      expect(screen.getByText(value)).toBeInTheDocument();
    }
    for (const n of ["n=31", "n=40", "n=44", "n=33", "n=35", "n=38", "n=8"]) {
      expect(screen.getByText(n)).toBeInTheDocument();
    }
    // Workload numbers, verbatim from the summary API.
    expect(screen.getByTestId("overs")).toHaveTextContent("13.5");
    expect(screen.getByTestId("ceiling")).toHaveTextContent("16");
    expect(screen.getByTestId("remaining")).toHaveTextContent("15");
    // No client-side rounding anywhere.
    expect(screen.queryByText("0.57")).not.toBeInTheDocument();
    expect(screen.queryByText("0.71")).not.toBeInTheDocument();
  });

  it("keeps the workload panel first and sticky (never below the fold)", async () => {
    visit("?player=p1");
    stubApi();
    render(<ProgressPage />);
    const view = await screen.findByTestId("parent-view");
    const panel = screen.getByTestId("workload-panel");
    expect(view.firstElementChild).toBe(panel);
    expect(panel).toHaveClass("sticky");
  });

  it("notes the suspect-session exclusion and flags unqualified trends", async () => {
    visit("?player=p1");
    stubApi();
    render(<ProgressPage />);
    await screen.findByTestId("parent-view");
    expect(screen.getByTestId("suspect-note")).toHaveTextContent(COPY.suspectNote);
    expect(screen.getByTestId("unqualified-note")).toHaveTextContent(COPY.unqualifiedNote);
  });

  it("shows both report cards", async () => {
    visit("?player=p1");
    stubApi();
    render(<ProgressPage />);
    await screen.findByTestId("parent-view");
    expect(screen.getByTestId("report-weekly")).toBeInTheDocument();
    expect(screen.getByTestId("report-monthly")).toBeInTheDocument();
  });

  it("renders a monthly-only lab without a weekly card", async () => {
    visit("?player=p1");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const payload = (() => {
          if (url.includes("kind=weekly")) return [];
          if (url.includes("kind=monthly")) return [monthly];
          if (url.includes("/milestones/")) return [];
          if (url.includes("/workload/")) return [];
          return { pain_active: false };
        })();
        return { ok: true, status: 200, json: async () => payload };
      }),
    );
    render(<ProgressPage />);
    await screen.findByTestId("parent-view");
    expect(screen.queryByTestId("report-weekly")).not.toBeInTheDocument();
    expect(screen.getByTestId("report-monthly")).toBeInTheDocument();
    expect(screen.queryByTestId("no-reports")).not.toBeInTheDocument();
    expect(screen.getByTestId("no-trends")).toBeInTheDocument();
  });
});

describe("ProgressPage kid mode (US-K4 SAF)", () => {
  async function renderKidMode(): Promise<void> {
    visit("?player=p1");
    stubApi();
    render(<ProgressPage />);
    await screen.findByTestId("parent-view");
    fireEvent.click(screen.getByRole("button", { name: COPY.toggleToKid }));
    await screen.findByTestId("kid-view");
  }

  it("celebrates milestones with no raw workload numbers anywhere", async () => {
    await renderKidMode();
    expect(screen.queryByTestId("workload-panel")).not.toBeInTheDocument();
    const text = document.body.textContent ?? "";
    expect(text).not.toContain("13.5");
    expect(text).not.toContain("overs");
    expect(screen.getByText(COPY.kidPersonalBest)).toBeInTheDocument();
    expect(screen.getByText(`3 ${COPY.kidStreakSuffix}`)).toBeInTheDocument();
  });

  it("frames the regression with coaching language, never a raw decline", async () => {
    await renderKidMode();
    expect(screen.getByTestId("coaching-frame")).toHaveTextContent(COPY.regressionFrame);
    const text = document.body.textContent ?? "";
    expect(text).not.toContain("0.49"); // the regressing series' numbers stay out
    expect(text).not.toContain("regressing");
  });

  it("omits the coaching frame when nothing regresses and toggles back", async () => {
    visit("?player=p1");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const payload = (() => {
          if (url.includes("kind=weekly")) return [];
          if (url.includes("kind=monthly")) return [];
          if (url.includes("/milestones/")) return milestones;
          if (url.includes("/workload/")) return [workload];
          return { pain_active: false };
        })();
        return { ok: true, status: 200, json: async () => payload };
      }),
    );
    render(<ProgressPage />);
    await screen.findByTestId("parent-view");
    fireEvent.click(screen.getByRole("button", { name: COPY.toggleToKid }));
    expect(await screen.findByTestId("kid-view")).toBeInTheDocument();
    expect(screen.queryByTestId("coaching-frame")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: COPY.toggleToParent }));
    expect(await screen.findByTestId("parent-view")).toBeInTheDocument();
  });
});

describe("ProgressPage honest states on the primitives", () => {
  it("announces the loading skeleton", () => {
    visit("?player=p1");
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise(() => undefined)),
    );
    render(<ProgressPage />);
    expect(screen.getByRole("status")).toHaveTextContent(COPY.loading);
    expect(screen.queryByRole("button", { name: COPY.toggleToKid })).not.toBeInTheDocument();
  });

  it("points a missing player to the sessions list", async () => {
    visit("");
    stubApi();
    render(<ProgressPage />);
    await screen.findByTestId("missing-player");
    expect(screen.getByRole("link", { name: COPY.pickPlayerAction })).toHaveAttribute(
      "href",
      "/sessions",
    );
  });

  it("retries a failed load and then shows the dashboard", async () => {
    visit("?player=p1");
    let calls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        calls += 1;
        if (calls <= 5) {
          throw new Error("connection refused");
        }
        const payload = url.includes("/wellness/") ? { pain_active: false } : [];
        return { ok: true, status: 200, json: async () => payload };
      }),
    );
    render(<ProgressPage />);
    await screen.findByTestId("load-failed");
    fireEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(await screen.findByTestId("parent-view")).toBeInTheDocument();
  });

  it("shows the report honesty banner verbatim in the degraded banner", async () => {
    visit("?player=p1");
    const banner = "Two sessions were left out: calibration warning.";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const payload = (() => {
          if (url.includes("kind=weekly"))
            return [{ ...weekly, body: { ...weekly.body, honesty_banner: banner } }];
          if (url.includes("/wellness/")) return { pain_active: false };
          return [];
        })();
        return { ok: true, status: 200, json: async () => payload };
      }),
    );
    render(<ProgressPage />);
    const region = await screen.findByRole("region", { name: "Incomplete data" });
    expect(region).toHaveTextContent(banner);
  });

  it("renames the page in kid mode and stays axe clean in both views", async () => {
    visit("?player=p1");
    stubApi();
    const { container } = render(<ProgressPage />);
    await screen.findByTestId("parent-view");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(COPY.title);
    await expectAxeClean(container);
    fireEvent.click(screen.getByRole("button", { name: COPY.toggleToKid }));
    await screen.findByTestId("kid-view");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(COPY.kidTitle);
    await expectAxeClean(container);
  });
});
