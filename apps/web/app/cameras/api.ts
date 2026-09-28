/**
 * Cameras feature client (US-A1/I1 registry and US-A4 health checks). Types
 * mirror `cricai_api.routers.cameras` and `cricai_api.routers.health_checks`
 * field for field; the screen renders them exactly as served.
 */

import { defaultConfig, type ApiConfig } from "@/lib/api";
import { request } from "../pipeline/http";

/** cricai_data.enums.CameraRole. */
export type CameraRole = "batting_side" | "bowling_side" | "wrist" | "front_on" | "other";

export const CAMERA_ROLES: readonly CameraRole[] = [
  "batting_side",
  "bowling_side",
  "wrist",
  "front_on",
  "other",
];

/** GET /cameras items (CameraOut). */
export interface CameraOut {
  id: string;
  camera_id: string;
  era_no: number;
  active: boolean;
  role: CameraRole | null;
  position_label: string;
  xyz_offset_m: Record<string, number>;
  height_m: number;
  fps: number;
  resolution: string;
  lens: string;
  mount: string;
  protected: boolean;
  fov_reference_key: string | null;
  reserved_for_future: boolean;
}

/** One probe inside a health-check record (cricai_data.healthcheck.CheckResult). */
export interface CheckResult {
  name: string;
  passed: boolean;
  detail: string;
}

/** GET /health-checks items (HealthCheckRecordOut). `results` is served as a
 * free-form object whose `checks` key carries the CheckResult list. */
export interface HealthCheckRecordOut {
  id: string;
  session_id: string | null;
  at: string;
  passed: boolean;
  results: { checks?: CheckResult[] } & Record<string, unknown>;
}

export interface CameraListParams {
  includeHistory?: boolean;
  role?: CameraRole;
}

export interface CamerasApi {
  listCameras(params?: CameraListParams): Promise<CameraOut[]>;
  setRole(cameraId: string, role: CameraRole | null): Promise<CameraOut>;
  listHealthChecks(sessionId?: string): Promise<HealthCheckRecordOut[]>;
}

export function createCamerasApi(config: ApiConfig = defaultConfig()): CamerasApi {
  return {
    listCameras: (params = {}) =>
      request<CameraOut[]>(config, "/cameras", {
        query: { include_history: params.includeHistory, role: params.role },
      }),
    setRole: (cameraId, role) =>
      request<CameraOut>(config, `/cameras/${cameraId}`, { method: "PATCH", body: { role } }),
    listHealthChecks: (sessionId) =>
      request<HealthCheckRecordOut[]>(config, "/health-checks", {
        query: { session_id: sessionId },
      }),
  };
}
