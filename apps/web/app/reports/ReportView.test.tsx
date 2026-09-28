/** US-K5: report surface — renders Report-body-v1 in the server HTML's exact
 * section order, incl. safety/coverage/fatigue/claims, honesty paths, and the
 * weekly/monthly trends/milestones keys. SAF: body strings render inert. */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { BattingSplit, BowlingSection, ReportBody } from "./api";
import ReportView from "./ReportView";

function emptyBody(overrides: Partial<ReportBody> = {}): ReportBody {
  return {
    kind: "daily",
    period: { start: "2026-07-09", end: "2026-07-09" },
    main_correction: null,
    drill: null,
    goal: null,
    secondary: [],
    positive: "Great effort today.",
    safety: null,
    honesty_banner: null,
    coverage_note: null,
    fatigue_note: null,
    claims: [],
    ...overrides,
  };
}

function fullBody(): ReportBody {
  return emptyBody({
    main_correction: {
      finding_id: "f-1",
      text: "Move your front foot to the ball. Seen on 12 balls.",
      evidence: { "3": { C1: "clip-a", C2: "clip-b" }, "4": { C1: "clip-c" } },
    },
    drill: {
      drill_id: "d-1",
      text: "Front-foot ladder drill. Do 30 balls.",
      machine_settings: {},
      success_metric: "control_pct",
    },
    goal: { metric: "control_pct", target: 70, condition: {} },
    secondary: [
      { finding_id: "f-2", text: "Late on the cut. (4 balls.)", evidence: { "9": { C1: "x" } } },
      { finding_id: "f-3", text: "Head still on defence.", evidence: {} },
    ],
    safety: { active: true, codes: ["workload_ceiling"], text: "Stop bowling today.", sha256: "s" },
    honesty_banner: "Thin data today - treat trends with care.",
    coverage_note: "One of two sessions had degraded footage.",
    fatigue_note: {
      text: "Energy dipped late - consider ending on a high.",
      window: 30,
      control_drop_points: 12.5,
      degrading_metrics: ["control_pct", "exit_velo"],
    },
    claims: [{ value: 12, metric: "ball_count", recompute_key: "finding:f-1:n" }],
    trends: [
      {
        metric: "control_pct",
        zone_key: "full/off",
        points: [
          { date: "2026-07-01", value: 61, n: 40 },
          { date: "2026-07-08", value: 66, n: 52 },
        ],
        direction: "improving",
        qualified: true,
      },
      { metric: "exit_velo", zone_key: null, points: [], direction: "flat", qualified: false },
    ],
    milestones: [{ kind: "personal_best", metric: "exit_velo", value: 92, achieved_on: "2026-07-08" }],
  });
}

/** A populated US-I7 bowling body block, as assemble_bowling_report_body ships it. */
function bowlingSection(): BowlingSection {
  return {
    accuracy_scorecard: {
      overall: { hits: 9, n: 15, pct: 60 },
      by_variation: [
        { variation: "googly", hits: 3, n: 5, pct: 60 },
        { variation: "leg_break", hits: 6, n: 10, pct: 60 },
      ],
      counted: 15,
      total: 18,
      note: "Accuracy is counted only over deliveries with a confident target call.",
    },
    release_scatter: {
      n: 15,
      mean_cm: 201.5,
      sigma_cm: 3.2,
      min_cm: 195.1,
      max_cm: 208,
      points: [
        { ball_id: 3, release_height_cm: 201.1, variation_intent: "leg_break" },
        { ball_id: 5, release_height_cm: 203.4, variation_intent: "googly" },
      ],
      by_variation: [
        { variation: "googly", n: 5, mean_cm: 202.9, sigma_cm: 2.1 },
        { variation: "leg_break", n: 10, mean_cm: 200.8, sigma_cm: 3.6 },
      ],
      note: "Scatter uses release height only for now.",
    },
    variation_agreement: {
      labeled: 18,
      compared: 15,
      unclear: 3,
      agreement_pct: 80,
      matrix: [
        { intent: "googly", detected: "unclear", count: 3 },
        { intent: "leg_break", detected: "leg_break", count: 12 },
      ],
      note: "Your declared intent is the ground truth.",
    },
    learning_modules: [
      {
        kind: "release_consistency",
        legend: "Shane Warne",
        title: "Same slot, every ball",
        principle: "Warne's fundamentals start with a repeatable action.",
        lesson: "Freeze each clip at the moment of release.",
        drill: "Shadow-bowl in front of a mirror.",
        approved_by: "pending_coach_review",
        finding_id: "f-b1",
        example_ball_ids: [3, 5],
        examples: { "3": { C7: "clip-x" }, "5": { C7: "clip-y" } },
      },
      {
        kind: "target_accuracy",
        legend: "Shane Warne",
        title: "Land it on the coin",
        principle: "Target discipline first, magic second.",
        lesson: "Pick the variation that missed most.",
        drill: "Place a marker on a good length.",
        approved_by: "pending_coach_review",
        finding_id: "f-b2",
        example_ball_ids: [],
        examples: {},
      },
    ],
    workload: {
      window: { start: "2026-07-03", end: "2026-07-09" },
      weighted_overs: 12,
      ceiling_overs: 20,
      remaining_balls: 48,
      violations: ["daily_overs_exceeded"],
    },
  };
}

/** The honest-minimum bowling block: no scored balls, no ceiling for the band. */
function sparseBowlingSection(): BowlingSection {
  return {
    accuracy_scorecard: {
      overall: { hits: 0, n: 0, pct: null },
      by_variation: [],
      counted: 0,
      total: 2,
      note: "Accuracy is counted only over deliveries with a confident target call.",
    },
    release_scatter: {
      n: 0,
      mean_cm: null,
      sigma_cm: null,
      min_cm: null,
      max_cm: null,
      points: [],
      by_variation: [],
      note: "Scatter uses release height only for now.",
    },
    variation_agreement: {
      labeled: 2,
      compared: 0,
      unclear: 2,
      agreement_pct: null,
      matrix: [{ intent: "unknown", detected: "unclear", count: 2 }],
      note: "Your declared intent is the ground truth.",
    },
    learning_modules: [],
    workload: {
      window: { start: "2026-07-03", end: "2026-07-09" },
      weighted_overs: 3.5,
      ceiling_overs: null,
      remaining_balls: null,
      violations: [],
    },
  };
}

/** The T5 #2 golden's US-H2 split: one over-threshold block and a skipped fun block. */
function battingSplit(): BattingSplit {
  return {
    intents: [
      { intent: "technical", planned_balls: 4, actual_balls: 3, deviation_pct: -25, flagged: false },
      { intent: "decision", planned_balls: 2, actual_balls: 3, deviation_pct: 50, flagged: true },
      { intent: "spin_specific", planned_balls: 3, actual_balls: 3, deviation_pct: 0, flagged: false },
      { intent: "fun", planned_balls: 1, actual_balls: 0, deviation_pct: -100, flagged: true },
    ],
    planned_total: 10,
    actual_total: 9,
    flagged_intents: ["decision", "fun"],
    fun_block_intact: false,
    note: "Planned vs actual balls per block for the day.",
  };
}

describe("ReportView", () => {
  it("renders every section of a full body in server order", () => {
    render(<ReportView body={fullBody()} />);
    expect(screen.getByRole("heading", { name: "daily report" })).toBeInTheDocument();
    expect(screen.getByText("2026-07-09 to 2026-07-09")).toBeInTheDocument();
    expect(screen.getByTestId("safety")).toHaveTextContent("Stop bowling today.");
    expect(screen.getByTestId("honesty-banner")).toHaveTextContent("Thin data today");
    expect(screen.getByTestId("coverage-note")).toHaveTextContent("degraded footage");
    expect(screen.getByText(/Move your front foot/)).toBeInTheDocument();
    expect(screen.getByText(/ball 3 - C1:/)).toBeInTheDocument();
    expect(screen.getByText("clip-b")).toBeInTheDocument();
    expect(screen.getByTestId("drill")).toHaveTextContent("Front-foot ladder drill. Do 30 balls.");
    expect(screen.getByTestId("goal")).toHaveTextContent("Control (%): reach 70 next session.");
    expect(screen.getByTestId("fatigue")).toHaveTextContent(
      "Last 30 balls · control drop 12.5 points · watching: control_pct, exit_velo",
    );
    expect(screen.getByText("Also worth a look")).toBeInTheDocument();
    expect(screen.getByText(/Late on the cut/)).toBeInTheDocument();
    expect(screen.getByTestId("positive")).toHaveTextContent("Great effort today.");
    expect(screen.getByTestId("claims")).toHaveTextContent("finding:f-1:n");
    expect(screen.getByTestId("trends")).toHaveTextContent(
      "control_pct (full/off): improving over 2 points",
    );
    // US-G5 guard: an unqualified series never prints a direction verdict.
    expect(screen.getByTestId("trends")).toHaveTextContent(
      "exit_velo: not enough data yet for a trend claim",
    );
    expect(screen.getByTestId("trends")).not.toHaveTextContent("flat");
    expect(screen.getByTestId("milestones")).toHaveTextContent(
      "personal_best: exit_velo 92 on 2026-07-08",
    );
    // safety precedes the honesty banner in the DOM (child never reads
    // reassurance above a safety block — mirrors render_report_html).
    const safety = screen.getByTestId("safety");
    const banner = screen.getByTestId("honesty-banner");
    expect(safety.compareDocumentPosition(banner) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("marks the net-wall core and leaves the detail off the wall sheet", () => {
    const { container } = render(<ReportView body={fullBody()} />);
    const wall = (testId: string) => screen.getByTestId(testId).getAttribute("data-wall");
    for (const core of ["safety", "honesty-banner", "coverage-note", "drill", "goal", "positive"]) {
      expect(wall(core), core).toBe("core");
    }
    for (const detail of ["fatigue", "claims", "trends", "milestones"]) {
      expect(wall(detail), detail).toBe("detail");
    }
    expect(screen.getByRole("heading", { name: "Main correction" }).closest("section")).toHaveAttribute(
      "data-wall",
      "core",
    );
    expect(container.querySelector("details.secondary")).toHaveAttribute("data-wall", "detail");
    // an active safety verdict is announced, not just printed
    expect(screen.getByTestId("safety")).toHaveAttribute("role", "alert");
  });

  it("renders a body that lacks optional keys without crashing", () => {
    const legacy = {
      kind: "daily",
      period: { start: "2026-07-09", end: "2026-07-09" },
      positive: "Great effort today.",
    } as unknown as ReportBody;
    render(<ReportView body={legacy} />);
    expect(screen.getByRole("heading", { name: "daily report" })).toBeInTheDocument();
    expect(screen.getByTestId("positive")).toHaveTextContent("Great effort today.");
    for (const id of ["safety", "honesty-banner", "coverage-note", "drill", "goal", "fatigue", "claims"]) {
      expect(screen.queryByTestId(id), id).not.toBeInTheDocument();
    }
    expect(screen.queryByText("Also worth a look")).not.toBeInTheDocument();
  });

  it("renders the honest empty body with only header and positive", () => {
    render(<ReportView body={emptyBody()} />);
    expect(screen.getByTestId("positive")).toBeInTheDocument();
    for (const missing of [
      "safety",
      "honesty-banner",
      "coverage-note",
      "drill",
      "goal",
      "fatigue",
      "claims",
      "bowling",
      "batting-split",
      "trends",
      "milestones",
    ]) {
      expect(screen.queryByTestId(missing)).not.toBeInTheDocument();
    }
    expect(screen.queryByText("Main correction")).not.toBeInTheDocument();
    expect(screen.queryByText("Also worth a look")).not.toBeInTheDocument();
  });

  it("hides inactive safety, words a coach-set goal, and skips empty lists", () => {
    render(
      <ReportView
        body={emptyBody({
          safety: { active: false, codes: [], text: "all clear", sha256: "s" },
          goal: { metric: "control_pct", target: null, condition: {} },
          fatigue_note: {
            text: "Fresh throughout.",
            window: 30,
            control_drop_points: 0,
            degrading_metrics: [],
          },
          trends: [],
          milestones: [],
        })}
      />,
    );
    expect(screen.queryByTestId("safety")).not.toBeInTheDocument();
    expect(screen.getByTestId("goal")).toHaveTextContent(
      "Control (%): reach coach-set next session.",
    );
    expect(screen.getByTestId("fatigue")).not.toHaveTextContent("watching:");
    expect(screen.queryByTestId("trends")).not.toBeInTheDocument();
    expect(screen.queryByTestId("milestones")).not.toBeInTheDocument();
  });

  it("renders the leg-spin bowling section when the body carries one (US-I7)", () => {
    render(<ReportView body={emptyBody({ bowling: bowlingSection() })} />);
    expect(screen.getByTestId("bowling")).toBeInTheDocument();
    // accuracy scorecard with honest denominators (US-I4)
    const accuracy = screen.getByTestId("bowling-accuracy");
    expect(accuracy).toHaveTextContent("9/15");
    expect(accuracy).toHaveTextContent("60%");
    expect(accuracy).toHaveTextContent("leg_break");
    expect(accuracy).toHaveTextContent("15 of 18 deliveries had a confident target call.");
    expect(accuracy).toHaveTextContent("counted only over deliveries with a confident target");
    // release scatter summary (US-I3)
    const scatter = screen.getByTestId("bowling-scatter");
    expect(scatter).toHaveTextContent("15 measured");
    expect(scatter).toHaveTextContent("mean 201.5 cm");
    expect(scatter).toHaveTextContent("sigma 3.2 cm");
    expect(scatter).toHaveTextContent("range 195.1 cm to 208 cm");
    expect(scatter).toHaveTextContent("googly: 5 measured");
    // T5 #4: the release scatter renders its per-ball points verbatim (US-K4)
    const points = screen.getByTestId("bowling-scatter-points").querySelectorAll("li");
    expect(points).toHaveLength(2);
    expect(points[0]).toHaveTextContent("ball 3: 201.1 cm (leg_break)");
    expect(points[1]).toHaveTextContent("ball 5: 203.4 cm (googly)");
    // intent-vs-detected agreement (US-I6)
    const agreement = screen.getByTestId("bowling-agreement");
    expect(agreement).toHaveTextContent("Agreement 80% over 15 compared");
    expect(agreement).toHaveTextContent("3 unclear of 18 declared");
    expect(agreement).toHaveTextContent("leg_break");
    expect(agreement).toHaveTextContent("12");
    // Warne/Saqlain learning modules with the player's OWN example balls
    const modules = screen.getByTestId("bowling-modules");
    expect(modules).toHaveTextContent("Same slot, every ball — Shane Warne");
    expect(modules).toHaveTextContent("repeatable action");
    expect(modules).toHaveTextContent("Your example balls: 3, 5");
    expect(modules).toHaveTextContent("Land it on the coin");
    // workload vs ceiling ALWAYS ships on a bowling body (US-I7 AC)
    const workload = screen.getByTestId("bowling-workload");
    expect(workload).toHaveTextContent(
      "12 overs bowled 2026-07-03 to 2026-07-09 of 20 allowed · 48 balls left",
    );
    expect(screen.getByTestId("workload-violations")).toHaveTextContent("daily_overs_exceeded");
  });

  it("renders sparse bowling data honestly: dashes, no fabricated numbers", () => {
    render(<ReportView body={emptyBody({ bowling: sparseBowlingSection() })} />);
    const accuracy = screen.getByTestId("bowling-accuracy");
    expect(accuracy).toHaveTextContent("0/0");
    expect(accuracy).toHaveTextContent("—");
    expect(accuracy).not.toHaveTextContent("0%");
    const scatter = screen.getByTestId("bowling-scatter");
    expect(scatter).toHaveTextContent("mean —");
    expect(scatter).toHaveTextContent("sigma —");
    // Zero measured releases: no points list at all, never fabricated dots.
    expect(screen.queryByTestId("bowling-scatter-points")).not.toBeInTheDocument();
    const agreement = screen.getByTestId("bowling-agreement");
    expect(agreement).toHaveTextContent("Not enough compared deliveries");
    expect(screen.queryByTestId("bowling-modules")).not.toBeInTheDocument();
    // ceiling-free age band stays honest, remaining stays unstated
    const workload = screen.getByTestId("bowling-workload");
    expect(workload).toHaveTextContent("3.5 overs bowled");
    expect(workload).toHaveTextContent("no overs ceiling applies for this age band");
    expect(workload).not.toHaveTextContent("balls left");
    expect(screen.queryByTestId("workload-violations")).not.toBeInTheDocument();
  });

  it("says so when the workload block itself is missing (honest null, US-I7)", () => {
    render(
      <ReportView body={emptyBody({ bowling: { ...sparseBowlingSection(), workload: null } })} />,
    );
    expect(screen.getByTestId("bowling-workload")).toHaveTextContent(
      "Workload data unavailable for this session.",
    );
  });

  it("links each evidence entry to the session player at that ball (US-K5)", () => {
    render(<ReportView body={fullBody()} sessionId="sess-42" />);
    const link = screen.getByRole("link", { name: "ball 3 - C1" });
    expect(link).toHaveAttribute("href", "/sessions/sess-42?ball=3");
    // the clip key still renders alongside the link
    expect(screen.getByText("clip-a")).toBeInTheDocument();
    // secondary evidence links too
    expect(screen.getByRole("link", { name: "ball 9 - C1" })).toHaveAttribute(
      "href",
      "/sessions/sess-42?ball=9",
    );
  });

  it("renders evidence as inert text when the body has no session id", () => {
    render(<ReportView body={fullBody()} sessionId={null} />);
    expect(screen.queryByRole("link", { name: /ball 3/ })).not.toBeInTheDocument();
    expect(screen.getByText(/ball 3 - C1:/)).toBeInTheDocument();
  });

  it("renders the US-H2 plan-vs-actual split table when the body carries one", () => {
    render(<ReportView body={emptyBody({ batting_split: battingSplit() })} />);
    const split = screen.getByTestId("batting-split");
    expect(split).toHaveTextContent("Plan vs actual");
    for (const intent of ["technical", "decision", "spin_specific", "fun"]) {
      expect(split).toHaveTextContent(intent);
    }
    expect(split).toHaveTextContent("50%");
    expect(split).toHaveTextContent("-100%");
    expect(split).toHaveTextContent("Planned 10 balls · 9 logged · fun block missing.");
    // flagged rows are marked so a coach can spot them at a glance
    const rows = split.querySelectorAll("tbody tr");
    expect(rows).toHaveLength(4);
    expect(split.querySelectorAll('tr[data-flagged="true"]')).toHaveLength(2);
    expect(split).toHaveTextContent(battingSplit().note);
  });

  it("reports the fun block as intact when it was played (US-H2 kid-first)", () => {
    const split = { ...battingSplit(), fun_block_intact: true };
    render(<ReportView body={emptyBody({ batting_split: split })} />);
    expect(screen.getByTestId("batting-split")).toHaveTextContent("fun block intact.");
  });

  it("renders hostile body strings inert (SAF)", () => {
    const payload = '<script>alert("pwn")</script>';
    const { container } = render(
      <ReportView body={emptyBody({ positive: payload, honesty_banner: payload })} />,
    );
    expect(screen.getAllByText(payload)).toHaveLength(2);
    expect(container.querySelector("script")).toBeNull();
  });
});
