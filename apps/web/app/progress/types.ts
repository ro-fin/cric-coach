/**
 * US-K4/US-G5 progress dashboard types — mirrors of the API wire shapes.
 *
 * `TrendSeries`/`BodyMilestone` are the pinned contract-#3 keys the rollup
 * job (`cricai_worker.rollup_reports`) writes into weekly/monthly report
 * bodies. The dashboard renders these numbers EXACTLY as received — it never
 * computes a metric client-side (US-K4 data-parity acceptance).
 */

export type TrendDirection = "improving" | "flat" | "regressing";

export type TrendPoint = {
  date: string;
  value: number;
  n: number;
};

export type TrendSeries = {
  metric: string;
  zone_key: string;
  points: TrendPoint[];
  direction: TrendDirection;
  qualified: boolean;
};

export type BodyMilestone = {
  kind: string;
  metric: string;
  value: number;
  achieved_on: string;
};

export type RollupBody = {
  kind: string;
  period: { start: string; end: string };
  positive: string;
  honesty_banner: string | null;
  trends: TrendSeries[];
  milestones: BodyMilestone[];
};

export type RollupReport = {
  id: string;
  kind: "weekly" | "monthly";
  status: string;
  period_start: string;
  period_end: string;
  body: RollupBody;
};

export type MilestoneKind = "personal_best" | "volume" | "streak";

export type Milestone = {
  id: string;
  kind: MilestoneKind;
  metric: string;
  value: number;
  context: Record<string, unknown>;
  achieved_on: string;
};

/** One rolling-7 window from `GET /workload/players/{id}/summary` (US-H1). */
export type WorkloadWindow = {
  window_start: string;
  window_end: string;
  weighted_balls: number;
  weighted_overs: number;
  bowling_days: string[];
  ceiling_overs: number | null;
  violations: string[];
  remaining_balls: number | null;
};

export type WellnessState = {
  pain_active: boolean;
};

/** Everything the progress page needs, loaded in one pass. */
export type DashboardData = {
  weekly: RollupReport | null;
  monthly: RollupReport | null;
  milestones: Milestone[];
  workload: WorkloadWindow | null;
  wellness: WellnessState;
};
