/**
 * T4 feature factories (pipeline, wellness, alerts, cameras, health checks,
 * settings). One factory per wire type, each taking partial overrides, in the
 * same style as T1's `fixtures.ts`. Values are realistic API answers so a test
 * can assert them verbatim on screen (data parity).
 */

import type { AlertOut } from "@/app/alerts/api";
import type { CameraOut, CheckResult, HealthCheckRecordOut } from "@/app/cameras/api";
import type {
  RunDetailOut,
  RunOut,
  RunSummaryOut,
  StageAttemptOut,
  StageOutcomeOut,
} from "@/app/pipeline/api";
import type { AppSettingsOut } from "@/app/settings/api";
import type { CheckinOut, ClearanceOut, WellnessStateOut } from "@/app/wellness/api";

export const OPS_SESSION_ID = "5e551010-0000-4000-8000-000000000001";
export const OPS_PLAYER_ID = "91a7e201-0000-4000-8000-000000000001";
export const OPS_RUN_ID = "a0000001-0000-4000-8000-000000000001";

// --- pipeline -------------------------------------------------------------

export function runSummary(o: Partial<RunSummaryOut> = {}): RunSummaryOut {
  return {
    id: OPS_RUN_ID,
    session_id: OPS_SESSION_ID,
    status: "succeeded",
    started_at: "2026-09-27T10:00:00Z",
    finished_at: "2026-09-27T10:04:31Z",
    ...o,
  };
}

export function stageAttempt(stage: string, o: Partial<StageAttemptOut> = {}): StageAttemptOut {
  return {
    stage,
    attempt: 1,
    status: "succeeded",
    input_digest: `sha256:${stage}-digest`,
    output: null,
    error: null,
    started_at: "2026-09-27T10:00:01Z",
    finished_at: "2026-09-27T10:00:09Z",
    ...o,
  };
}

export function runDetail(o: Partial<RunDetailOut> = {}): RunDetailOut {
  return {
    id: OPS_RUN_ID,
    session_id: OPS_SESSION_ID,
    status: "succeeded",
    detector_context: { detector: "ball-yolo-v3" },
    started_at: "2026-09-27T10:00:00Z",
    finished_at: "2026-09-27T10:04:31Z",
    stages: [stageAttempt("probe"), stageAttempt("events")],
    ...o,
  };
}

export function stageOutcome(stage: string, o: Partial<StageOutcomeOut> = {}): StageOutcomeOut {
  return {
    stage,
    status: "succeeded",
    attempts: 1,
    resumed: false,
    error: null,
    digest: `sha256:${stage}-digest`,
    ...o,
  };
}

export function runOut(o: Partial<RunOut> = {}): RunOut {
  return {
    run_id: OPS_RUN_ID,
    session_id: OPS_SESSION_ID,
    status: "succeeded",
    stages: [stageOutcome("probe"), stageOutcome("events")],
    ...o,
  };
}

// --- wellness -------------------------------------------------------------

export function checkin(o: Partial<CheckinOut> = {}): CheckinOut {
  return {
    id: "c0000001-0000-4000-8000-000000000001",
    player_id: OPS_PLAYER_ID,
    session_id: null,
    checkin_date: "2026-09-27",
    soreness: { shoulder_right: 1 },
    energy: 4,
    sleep_hours: 8.5,
    pain: false,
    pain_note: null,
    created_by: "player",
    created_at: "2026-09-27T07:30:00Z",
    ...o,
  };
}

export function clearance(o: Partial<ClearanceOut> = {}): ClearanceOut {
  return {
    id: "d0000001-0000-4000-8000-000000000001",
    checkin_id: "c0000001-0000-4000-8000-000000000001",
    cleared_by: "parent",
    role: "parent",
    note: "Rested two days, no pain on throwing.",
    created_at: "2026-09-28T08:00:00Z",
    ...o,
  };
}

export function wellnessState(o: Partial<WellnessStateOut> = {}): WellnessStateOut {
  return {
    as_of: "2026-09-28",
    checked_in: true,
    no_checkin: false,
    last_checkin_date: "2026-09-28",
    days_since_checkin: 0,
    pain_active: false,
    bowling_suppressed: false,
    open_pain_checkin_ids: [],
    escalation: false,
    pain_reports_in_window: 0,
    ...o,
  };
}

// --- alerts ---------------------------------------------------------------

export function alert(o: Partial<AlertOut> = {}): AlertOut {
  return {
    id: "e0000001-0000-4000-8000-000000000001",
    audience: "parent",
    code: "camera_degraded",
    severity: "warning",
    detail: { camera_id: "C3", reason: "dropped frames" },
    session_id: OPS_SESSION_ID,
    acknowledged: false,
    created_at: "2026-09-27T10:05:00Z",
    ...o,
  };
}

// --- cameras and health checks ----------------------------------------------

export function camera(cameraId = "C1", o: Partial<CameraOut> = {}): CameraOut {
  return {
    id: `f0000000-0000-4000-8000-00000000000${cameraId.slice(1)}`,
    camera_id: cameraId,
    era_no: 1,
    active: true,
    role: "batting_side",
    position_label: "square leg, head height",
    xyz_offset_m: { x: 1.5, y: -4.2, z: 1.6 },
    height_m: 1.6,
    fps: 120,
    resolution: "1920x1080",
    lens: "wide",
    mount: "tripod",
    protected: false,
    fov_reference_key: null,
    reserved_for_future: false,
    ...o,
  };
}

export function checkResult(name: string, o: Partial<CheckResult> = {}): CheckResult {
  return { name, passed: true, detail: `${name} ok`, ...o };
}

export function healthCheck(o: Partial<HealthCheckRecordOut> = {}): HealthCheckRecordOut {
  return {
    id: "b0000001-0000-4000-8000-000000000001",
    session_id: OPS_SESSION_ID,
    at: "2026-09-27T09:55:00Z",
    passed: true,
    results: { checks: [checkResult("feed:C1"), checkResult("disk")] },
    ...o,
  };
}

// --- settings -------------------------------------------------------------

export function appSettings(o: Partial<AppSettingsOut> = {}): AppSettingsOut {
  return {
    version: 1,
    settings: {
      report_review: { mode: "auto_publish", timeout_hours: 24 },
      live_mode: {
        enabled: false,
        allowlist: ["ball_count", "target_hit_tally", "workload_remaining_balls", "fatigue_nudge"],
      },
    },
    approved_by: "coach",
    reason: "initial lab setup",
    created_at: "2026-09-20T09:00:00Z",
    ...o,
  };
}
