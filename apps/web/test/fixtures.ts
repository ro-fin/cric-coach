/**
 * Shared wire-shape factories (T1, phase-8-team.md §3.4).
 *
 * Every factory returns a field-for-field valid instance of the lib/api wire
 * type with sensible defaults, then applies the caller's overrides last. Keep
 * them server-data-faithful: a fixture is a sample of what cricai_api really
 * returns, never a convenient approximation.
 *
 * Feature-specific factories live in fixtures.core.ts (T3) and
 * fixtures.ops.ts (T4); this file holds only the lib/api types.
 */

import type {
  ClipOut,
  EventOut,
  MetricPhase,
  MetricValue,
  PhaseMetricsOut,
  ReviewQueueItemOut,
  SessionOut,
  SessionPage,
  TagOut,
  VideoOut,
} from "@/lib/api";

export const SESSION_ID = "s1";
export const PLAYER_ID = "p1";
export const SESSION_DATE = "2026-07-09";

/** GET /sessions/{id} (SessionOut). */
export function session(overrides: Partial<SessionOut> = {}): SessionOut {
  return {
    id: SESSION_ID,
    player_id: PLAYER_ID,
    session_date: SESSION_DATE,
    session_type: "batting",
    bowler_source: "machine",
    machine_settings: { speed_kph: 100, length: "good" },
    notes: null,
    state: "analyzed",
    degraded: false,
    missing_views: [],
    ...overrides,
  };
}

/** GET /sessions (SessionPage) wrapping the given items. */
export function sessionPage(
  items: SessionOut[],
  overrides: Partial<Omit<SessionPage, "items">> = {},
): SessionPage {
  return { items, total: items.length, limit: 50, offset: 0, ...overrides };
}

/** GET /sessions/{id}/tags item (TagOut). */
export function tag(ballNo: number, overrides: Partial<TagOut> = {}): TagOut {
  return {
    ball_no: ballNo,
    block_no: 1,
    line: "off",
    length: "good",
    shot: "drive",
    footwork: "front",
    contact: "middle",
    outcome: "controlled_ground_shot",
    control: true,
    source: "manual",
    ground_truth_eligible: true,
    created_by: "parent",
    audits: [],
    ...overrides,
  };
}

/** GET /sessions/{id}/events item (EventOut); timings are 10 s apart per ball. */
export function event(ballNo: number, overrides: Partial<EventOut> = {}): EventOut {
  const start = ballNo * 10_000;
  return {
    id: `e${ballNo}`,
    session_id: SESSION_ID,
    ball_no: ballNo,
    start_ms: start,
    release_ms: start + 500,
    contact_ms: null,
    end_ms: start + 6000,
    confidence: 0.9,
    source: "auto",
    detector_version: "v1",
    valid: true,
    created_at: `${SESSION_DATE}T10:00:00Z`,
    ...overrides,
  };
}

/** GET /sessions/{id}/clips item (ClipOut), cut and aligned with event(ballNo). */
export function clip(
  ballNo: number,
  cameraId: string,
  overrides: Partial<ClipOut> = {},
): ClipOut {
  const start = ballNo * 10_000;
  return {
    id: `c${ballNo}-${cameraId}`,
    session_id: SESSION_ID,
    ball_no: ballNo,
    camera_id: cameraId,
    object_key: `clips/b${ballNo}_${cameraId}.mp4`,
    start_ms: start,
    end_ms: start + 6000,
    status: "cut",
    error: null,
    ...overrides,
  };
}

/** GET /sessions/{id}/videos item (VideoOut), probed at 120 fps. */
export function video(cameraId: string, overrides: Partial<VideoOut> = {}): VideoOut {
  return {
    id: `v-${cameraId}`,
    session_id: SESSION_ID,
    camera_id: cameraId,
    object_key: `videos/${SESSION_ID}/${cameraId}.mp4`,
    filename: `${cameraId}.mp4`,
    checksum_sha256: "c".repeat(64),
    size_bytes: 1024,
    claimed_fps: 120,
    claimed_resolution: "1920x1080",
    claimed_duration_s: 600,
    codec: "h264",
    status: "probed",
    probe: null,
    error: null,
    ...overrides,
  };
}

/** One metric under the pinned {value, unit, confidence, reason} contract. */
export function metricValue(
  value: MetricValue["value"],
  overrides: Partial<MetricValue> = {},
): MetricValue {
  return { value, unit: "deg", confidence: 0.85, reason: null, ...overrides };
}

/** GET /sessions/{id}/balls/{n}/metrics item (PhaseMetricsOut). */
export function phaseMetrics(
  phase: MetricPhase,
  metrics: Record<string, MetricValue>,
  overrides: Partial<PhaseMetricsOut> = {},
): PhaseMetricsOut {
  return { phase, metrics, schema_version: 1, stored: true, ...overrides };
}

/** GET /settings/review-queue item (ReviewQueueItemOut). */
export function reviewItem(overrides: Partial<ReviewQueueItemOut> = {}): ReviewQueueItemOut {
  return {
    id: "r1",
    player_id: PLAYER_ID,
    session_id: SESSION_ID,
    kind: "daily",
    period_start: SESSION_DATE,
    period_end: SESSION_DATE,
    review_due_at: "2026-07-10T18:00:00Z",
    created_at: `${SESSION_DATE}T19:00:00Z`,
    ...overrides,
  };
}
