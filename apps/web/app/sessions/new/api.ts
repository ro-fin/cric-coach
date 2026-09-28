/**
 * New-session lifecycle client (US-A3, US-A5, US-B1): create the session, pass
 * the machine safety gate, start recording on the chosen cameras, stop.
 *
 * Field-for-field mirrors of the router models, pinned from source:
 * - `GET /players` -> list[PlayerOut] (routers/players.py, shared with Today);
 * - `POST /sessions` SessionIn -> SessionOut (routers/sessions.py; the date
 *   field travels as `date`; machine sessions must carry machine_settings);
 * - `GET /checklists/machine` -> MachineChecklistOut, `POST
 *   /sessions/{id}/checklist-ack` ChecklistAckIn -> ChecklistAckOut
 *   (routers/checklists.py; parent only, every item must be true);
 * - `GET /cameras` -> list[CameraOut] (routers/cameras.py; active eras only);
 * - `POST /sessions/{id}/start` StartIn -> StartOut, `POST /sessions/{id}/stop`
 *   StopIn -> StopOut, `GET /sessions/{id}/lifecycle` -> LifecycleOut
 *   (routers/lifecycle.py).
 *
 * The server decides every verdict (warnings, degraded, missing_views); this
 * module only carries them. Calls go through lib/api `apiRequest`, whose
 * ApiError keeps the server detail verbatim, including the structured 422
 * bodies (`message` plus the offending ids).
 */

import { apiRequest, defaultConfig } from "@/lib/api";
import type {
  ApiConfig,
  BowlerSource,
  LengthZone,
  SessionOut,
  SessionState,
  SessionType,
} from "@/lib/api";
import type { PlayerOut } from "@/app/_today/api";

export interface MachineSettingsIn {
  speed_kph: number;
  length: LengthZone;
  variation: string | null;
}

/** routers/sessions.py SessionIn (by alias: `date`). */
export interface SessionIn {
  player_id: string;
  date: string;
  session_type: SessionType;
  bowler_source: BowlerSource;
  machine_settings: MachineSettingsIn | null;
  notes: string | null;
}

export interface ChecklistItemOut {
  id: string;
  label: string;
}

export interface MachineChecklistOut {
  items: ChecklistItemOut[];
}

export interface ChecklistAckIn {
  items: Record<string, boolean>;
  acked_by: string;
}

export interface ChecklistAckOut {
  id: string;
  items: Record<string, boolean>;
  acked_by: string;
  acked_at: string;
}

export type CameraRole = "batting_side" | "bowling_side" | "wrist";

/** routers/cameras.py CameraOut. */
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

export interface StartOut {
  state: SessionState;
  started_at: string | null;
  cameras: string[];
  warnings: string[];
  idempotent: boolean;
}

export interface StopIn {
  cameras_reporting: Record<string, { ok: boolean }>;
}

export interface StopOut {
  state: SessionState;
  degraded: boolean;
  missing_views: string[];
  stopped_at: string | null;
  idempotent: boolean;
}

export interface LifecycleOut {
  state: SessionState;
  degraded: boolean;
  missing_views: string[];
  started_at: string | null;
  stopped_at: string | null;
}

export interface NewSessionApi {
  listPlayers(): Promise<PlayerOut[]>;
  createSession(payload: SessionIn): Promise<SessionOut>;
  getSession(sessionId: string): Promise<SessionOut>;
  machineChecklist(): Promise<MachineChecklistOut>;
  ackChecklist(sessionId: string, payload: ChecklistAckIn): Promise<ChecklistAckOut>;
  listCameras(): Promise<CameraOut[]>;
  start(sessionId: string, cameras: string[]): Promise<StartOut>;
  stop(sessionId: string, payload: StopIn): Promise<StopOut>;
  lifecycle(sessionId: string): Promise<LifecycleOut>;
}

export function createNewSessionApi(config: ApiConfig = defaultConfig()): NewSessionApi {
  const id = (sessionId: string) => encodeURIComponent(sessionId);
  const get = <T>(path: string) => apiRequest<T>(path, {}, config);
  const post = <T>(path: string, body: unknown) =>
    apiRequest<T>(path, { method: "POST", body }, config);
  return {
    listPlayers: () => get("/players"),
    createSession: (payload) => post("/sessions", payload),
    getSession: (sessionId) => get(`/sessions/${id(sessionId)}`),
    machineChecklist: () => get("/checklists/machine"),
    ackChecklist: (sessionId, payload) => post(`/sessions/${id(sessionId)}/checklist-ack`, payload),
    listCameras: () => get("/cameras"),
    start: (sessionId, cameras) => post(`/sessions/${id(sessionId)}/start`, { cameras }),
    stop: (sessionId, payload) => post(`/sessions/${id(sessionId)}/stop`, payload),
    lifecycle: (sessionId) => get(`/sessions/${id(sessionId)}/lifecycle`),
  };
}
