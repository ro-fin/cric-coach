/**
 * T3 (core screens) wire-shape factories. Each takes partial overrides and
 * returns a complete, valid response object mirroring the router model.
 * Shared factories (session, tag, event, clip, video, reviewItem) live in
 * T1's `fixtures.ts`; only feature shapes are defined here.
 */

import type { PlayerOut, ReportOut, WindowSummaryOut } from "@/app/_today/api";
import type { CorrectionItem, DrillPlan, Goal, ReportBody } from "@/app/reports/api";

export function player(o: Partial<PlayerOut> = {}): PlayerOut {
  return {
    id: "p1",
    name: "Arjun",
    birthdate: "2014-05-01",
    handedness: "right",
    is_guest: false,
    ...o,
  };
}

export function correction(o: Partial<CorrectionItem> = {}): CorrectionItem {
  return {
    finding_id: "f1",
    text: "Keep your head still over the ball at contact.",
    evidence: { "7": { cam_side: "clip-7-side" }, "12": { cam_front: "clip-12-front" } },
    ...o,
  };
}

export function drill(o: Partial<DrillPlan> = {}): DrillPlan {
  return {
    drill_id: "d1",
    text: "Tennis-ball head-still drill, 3 sets of 12.",
    machine_settings: {},
    success_metric: "head_movement_cm",
    ...o,
  };
}

export function goal(o: Partial<Goal> = {}): Goal {
  return { metric: "head_movement_cm", target: 4, condition: {}, ...o };
}

export function reportBody(o: Partial<ReportBody> = {}): ReportBody {
  return {
    kind: "daily",
    period: { start: "2026-09-27", end: "2026-09-27" },
    main_correction: correction(),
    drill: drill(),
    goal: goal(),
    secondary: [],
    positive: "Great balance on the front foot today.",
    safety: null,
    honesty_banner: null,
    coverage_note: null,
    fatigue_note: null,
    claims: [],
    ...o,
  };
}

export function dailyReport(o: Partial<ReportOut> = {}): ReportOut {
  return {
    id: "r1",
    player_id: "p1",
    session_id: "s1",
    kind: "daily",
    period_start: "2026-09-27",
    period_end: "2026-09-27",
    status: "published",
    body: reportBody(),
    quality: null,
    created_at: "2026-09-27T18:00:00Z",
    ...o,
  };
}

export function workloadWindow(o: Partial<WindowSummaryOut> = {}): WindowSummaryOut {
  return {
    window_start: "2026-09-22",
    window_end: "2026-09-28",
    weighted_balls: 96,
    weighted_overs: 16,
    bowling_days: ["2026-09-24", "2026-09-26"],
    consecutive_day_pairs: 0,
    band_max_age: 12,
    ceiling_overs: 24,
    violations: [],
    remaining_balls: 48,
    ...o,
  };
}
