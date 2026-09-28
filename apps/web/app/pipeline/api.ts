/**
 * Pipeline feature client (US-J1/L1 dashboard half). Every type below is a
 * field-for-field mirror of `cricai_api.routers.pipeline` response models;
 * the screen renders exactly what the API returns (US-K4 data parity).
 *
 * Built over the shared `ApiConfig`/`ApiError` from lib/api so the same-origin
 * proxy configuration (`defaultConfig()`) applies here untouched.
 */

import { ApiError, apiRequest, defaultConfig, detailText, type ApiConfig } from "@/lib/api";

// ---------------------------------------------------------------------------
// Enum vocabularies (cricai_data.enums / cricai_worker.pipeline wire values).
// ---------------------------------------------------------------------------

/** Pipeline run and per-stage lifecycle (cricai_data.enums.StageStatus). */
export type StageStatus = "pending" | "running" | "succeeded" | "failed" | "skipped";

/** Canonical stage order (cricai_worker.pipeline.STAGES) — display order only;
 * the API already serves stage rows sorted by it. */
export const STAGES: readonly string[] = [
  "probe",
  "calibrate_check",
  "events",
  "clips",
  "pose",
  "detect_track",
  "metrics",
  "bowling_action",
  "bowling_flight",
  "classify_variations",
  "analysis",
  "progress",
  "planner",
  "safety",
  "report",
];

// ---------------------------------------------------------------------------
// Response shapes.
// ---------------------------------------------------------------------------

/** GET /pipeline/sessions/{id}/runs items (RunSummaryOut). */
export interface RunSummaryOut {
  id: string;
  session_id: string;
  status: StageStatus;
  started_at: string;
  finished_at: string | null;
}

/** One stage attempt inside a run trace (StageAttemptOut). */
export interface StageAttemptOut {
  stage: string;
  attempt: number;
  status: StageStatus;
  input_digest: string | null;
  output: Record<string, unknown> | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

/** GET /pipeline/runs/{run_id} (RunDetailOut). */
export interface RunDetailOut {
  id: string;
  session_id: string;
  status: StageStatus;
  detector_context: Record<string, unknown>;
  started_at: string;
  finished_at: string | null;
  stages: StageAttemptOut[];
}

/** Per-stage outcome of a synchronous run (StageOutcomeOut). */
export interface StageOutcomeOut {
  stage: string;
  status: StageStatus;
  attempts: number;
  resumed: boolean;
  error: string | null;
  digest: string | null;
}

/** POST /pipeline/sessions/{id}/runs and /resume (RunOut). */
export interface RunOut {
  run_id: string;
  session_id: string;
  status: string;
  stages: StageOutcomeOut[];
}

/** POST /pipeline/sessions/{id}/rederive (RederiveOut). */
export interface RederiveOut {
  session_id: string;
  counts: Record<string, unknown>;
  manual_invalidation: boolean;
  run: RunOut | null;
  run_locked: boolean;
}

/** Request bodies (TriggerIn, RederiveIn). */
export interface TriggerIn {
  detector_context?: Record<string, unknown>;
}

export interface RederiveIn {
  confirm_manual_invalidation?: boolean;
  start_run?: boolean;
}

/** A re-derive either ran, or was refused because manual rows would be lost:
 * the router answers 409 with `{message, blockers}` and modifies nothing. */
export type RederiveResult =
  | { kind: "done"; out: RederiveOut }
  | { kind: "blocked"; message: string; blockers: Record<string, number> };

/** The structured 409 body of a blocked re-derive, or null for any other body. */
export function blockedDetail(body: unknown): { message: string; blockers: Record<string, number> } | null {
  if (typeof body !== "object" || body === null) {
    return null;
  }
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail !== "object" || detail === null) {
    return null;
  }
  const { message, blockers } = detail as { message?: unknown; blockers?: unknown };
  if (typeof message !== "string" || typeof blockers !== "object" || blockers === null) {
    return null;
  }
  return { message, blockers: blockers as Record<string, number> };
}

// ---------------------------------------------------------------------------
// Client.
// ---------------------------------------------------------------------------

export interface PipelineApi {
  listRuns(sessionId: string): Promise<RunSummaryOut[]>;
  runDetail(runId: string): Promise<RunDetailOut>;
  triggerRun(sessionId: string, body?: TriggerIn): Promise<RunOut>;
  resumeRun(sessionId: string, body?: TriggerIn): Promise<RunOut>;
  rederive(sessionId: string, body?: RederiveIn): Promise<RederiveResult>;
}

export function createPipelineApi(config: ApiConfig = defaultConfig()): PipelineApi {
  return {
    listRuns: (sessionId) =>
      apiRequest<RunSummaryOut[]>(`/pipeline/sessions/${sessionId}/runs`, {}, config),
    runDetail: (runId) => apiRequest<RunDetailOut>(`/pipeline/runs/${runId}`, {}, config),
    triggerRun: (sessionId, body = {}) =>
      apiRequest<RunOut>(`/pipeline/sessions/${sessionId}/runs`, { method: "POST", body }, config),
    resumeRun: (sessionId, body = {}) =>
      apiRequest<RunOut>(`/pipeline/sessions/${sessionId}/resume`, { method: "POST", body }, config),
    rederive: async (sessionId, body = {}) => {
      // Not apiRequest: a blocked re-derive is a decision to show (409 with
      // structured blockers), not a transport error.
      const fetchFn = config.fetchFn ?? fetch;
      const headers: Record<string, string> = {
        Accept: "application/json",
        "Content-Type": "application/json",
      };
      if (config.token) {
        headers.Authorization = `Bearer ${config.token}`;
      }
      const url = `${config.baseUrl.replace(/\/$/, "")}/pipeline/sessions/${sessionId}/rederive`;
      const response = await fetchFn(url, { method: "POST", headers, body: JSON.stringify(body) });
      let parsed: unknown;
      try {
        parsed = await response.json();
      } catch {
        parsed = undefined; // non-JSON body: fall through to the status text
      }
      if (response.ok) {
        return { kind: "done", out: parsed as RederiveOut };
      }
      const blocked = response.status === 409 ? blockedDetail(parsed) : null;
      if (blocked !== null) {
        return { kind: "blocked", ...blocked };
      }
      throw new ApiError(response.status, detailText(parsed, response.statusText));
    },
  };
}
