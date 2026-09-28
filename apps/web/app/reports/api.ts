/**
 * US-K5 reports API client over the pinned Report-body-v1 shape (cross-group
 * contract; weekly/monthly bodies add the optional `trends`/`milestones`
 * keys; a leg-spin session body adds the additive `bowling` key, US-I7).
 * The surface renders ONLY what the API returns — no client-side metric
 * computation (US-K4 data parity).
 *
 * Base URL and bearer token come from the single lib/api convention
 * (NEXT_PUBLIC_API_BASE_URL + NEXT_PUBLIC_API_TOKEN; see apps/web/README.md).
 * fetchReport throws a typed ApiError on non-2xx so callers can tell a server
 * refusal (403/404: honest error, never the offline cache) from an actual
 * network failure (fetch rejection: offline cache allowed).
 */

import { ApiError, apiBase, authHeaders } from "@/lib/api";

export interface Claim {
  value: number;
  metric: string;
  recompute_key: string;
}

/** ball_no -> camera_id -> clip_id (contract #2 evidence links). */
export type EvidenceMap = Record<string, Record<string, string>>;

export interface CorrectionItem {
  finding_id: string;
  text: string;
  evidence: EvidenceMap;
}

export interface DrillPlan {
  drill_id: string | null;
  text: string;
  machine_settings: Record<string, unknown>;
  success_metric: string;
}

export interface Goal {
  metric: string;
  target: number | null;
  condition: Record<string, unknown>;
}

export interface SafetyVerdict {
  active: boolean;
  codes: string[];
  text: string;
  sha256: string;
}

export interface FatigueNote {
  text: string;
  window: number;
  control_drop_points: number;
  degrading_metrics: string[];
}

export interface TrendPoint {
  date: string;
  value: number;
  n: number;
}

export interface Trend {
  metric: string;
  zone_key: string | null;
  points: TrendPoint[];
  direction: string;
  qualified: boolean;
}

export interface MilestoneItem {
  kind: string;
  metric: string;
  value: number;
  achieved_on: string;
}

// ---------------------------------------------------------------------------
// US-I7 bowling section (mirror of cricai_coaching.bowling_report's
// assemble_bowling_report_body additive `bowling` key).
// ---------------------------------------------------------------------------

/** One accuracy cell: hits over an honest denominator (US-I4). */
export interface AccuracyCell {
  hits: number;
  n: number;
  pct: number | null;
}

export interface VariationAccuracy extends AccuracyCell {
  variation: string;
}

export interface AccuracyScorecard {
  overall: AccuracyCell;
  by_variation: VariationAccuracy[];
  counted: number;
  total: number;
  note: string;
}

export interface ScatterPoint {
  ball_id: number;
  release_height_cm: number;
  variation_intent: string;
}

/** Release-height stats; nulls are honest (a 1-ball sigma would fake it). */
export interface ScatterStats {
  n: number;
  mean_cm: number | null;
  sigma_cm: number | null;
}

export interface VariationScatter extends ScatterStats {
  variation: string;
}

export interface ReleaseScatter extends ScatterStats {
  min_cm: number | null;
  max_cm: number | null;
  points: ScatterPoint[];
  by_variation: VariationScatter[];
  note: string;
}

export interface AgreementCell {
  intent: string;
  detected: string;
  count: number;
}

/** Intent-vs-detected block (US-I6): honest matrix, honest denominators. */
export interface VariationAgreement {
  labeled: number;
  compared: number;
  unclear: number;
  agreement_pct: number | null;
  matrix: AgreementCell[];
  note: string;
}

/** Coach-approved Warne/Saqlain module linked to the player's OWN balls. */
export interface LearningModule {
  kind: string;
  legend: string;
  title: string;
  principle: string;
  lesson: string;
  drill: string;
  approved_by: string;
  finding_id: string;
  example_ball_ids: number[];
  examples: EvidenceMap;
}

/** Week-to-date bowling workload vs the US-H1 ceiling (US-I7 AC). */
export interface BowlingWorkload {
  window: { start: string; end: string };
  weighted_overs: number;
  ceiling_overs: number | null;
  remaining_balls: number | null;
  violations: string[];
}

export interface BowlingSection {
  accuracy_scorecard: AccuracyScorecard;
  release_scatter: ReleaseScatter;
  variation_agreement: VariationAgreement;
  learning_modules: LearningModule[];
  workload: BowlingWorkload | null;
}

// ---------------------------------------------------------------------------
// US-H2 batting plan-vs-actual split (mirror of cricai_coaching.report's
// additive `batting_split` key on batting/MIXED bodies with tagged blocks).
// ---------------------------------------------------------------------------

/** One block intent's plan-vs-actual row (US-H2). */
export interface SplitIntent {
  intent: string;
  planned_balls: number;
  actual_balls: number;
  deviation_pct: number;
  flagged: boolean;
}

export interface BattingSplit {
  intents: SplitIntent[];
  planned_total: number;
  actual_total: number;
  flagged_intents: string[];
  fun_block_intact: boolean;
  note: string;
}

export interface ReportBody {
  kind: string;
  period: { start: string; end: string };
  main_correction: CorrectionItem | null;
  drill: DrillPlan | null;
  goal: Goal | null;
  secondary: CorrectionItem[];
  positive: string;
  safety: SafetyVerdict | null;
  honesty_banner: string | null;
  coverage_note: string | null;
  fatigue_note: FatigueNote | null;
  claims: Claim[];
  trends?: Trend[];
  milestones?: MilestoneItem[];
  bowling?: BowlingSection;
  batting_split?: BattingSplit;
}

export interface Report {
  id: string;
  player_id: string;
  session_id: string | null;
  kind: string;
  period_start: string;
  period_end: string;
  status: string;
  body: ReportBody;
  created_at: string;
}

export async function fetchReport(id: string): Promise<Report> {
  const response = await fetch(`${apiBase()}/reports/${id}`, { headers: authHeaders() });
  if (!response.ok) throw new ApiError(response.status, "report fetch failed");
  return (await response.json()) as Report;
}

/** Server-side PDF/PNG export endpoint for this report (US-K5). */
export function exportUrl(id: string, format: "pdf" | "png"): string {
  return `${apiBase()}/reports/${id}/export?format=${format}`;
}
