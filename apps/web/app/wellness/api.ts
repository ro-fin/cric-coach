/**
 * Wellness feature client (US-H4 dashboard half). Types mirror
 * `cricai_api.routers.wellness` field for field; the screen renders the pain
 * state machine's verdict exactly as served (US-K4 data parity).
 */

import { apiRequest, defaultConfig, type ApiConfig } from "@/lib/api";

/** Server bounds (cricai_coaching.wellness), mirrored so the form can explain
 * a value before posting it. The server stays the authority: its 422 problem
 * list is shown verbatim. */
export const SORENESS_LEVEL_MIN = 0;
export const SORENESS_LEVEL_MAX = 3;
export const ENERGY_MIN = 1;
export const ENERGY_MAX = 5;
export const SLEEP_HOURS_MIN = 0;
export const SLEEP_HOURS_MAX = 14;

/** cricai_coaching.wellness.SORENESS_BODY_KEYS, in head-to-foot order. */
export const SORENESS_BODY_KEYS: readonly string[] = [
  "neck",
  "shoulder_left",
  "shoulder_right",
  "back_upper",
  "back_lower",
  "side_left",
  "side_right",
  "elbow_left",
  "elbow_right",
  "wrist_left",
  "wrist_right",
  "hand_left",
  "hand_right",
  "hip_left",
  "hip_right",
  "groin",
  "hamstring_left",
  "hamstring_right",
  "quad_left",
  "quad_right",
  "knee_left",
  "knee_right",
  "calf_left",
  "calf_right",
  "shin_left",
  "shin_right",
  "ankle_left",
  "ankle_right",
  "foot_left",
  "foot_right",
];

/** POST /wellness/{player}/checkins body (CheckinIn). */
export interface CheckinIn {
  checkin_date: string;
  session_id?: string | null;
  soreness?: Record<string, number>;
  energy?: number | null;
  sleep_hours?: number | null;
  pain?: boolean;
  pain_note?: string | null;
}

/** GET /wellness/{player}/checkins items (CheckinOut). */
export interface CheckinOut {
  id: string;
  player_id: string;
  session_id: string | null;
  checkin_date: string;
  soreness: Record<string, number>;
  energy: number | null;
  sleep_hours: number | null;
  pain: boolean;
  pain_note: string | null;
  created_by: string;
  created_at: string;
}

/** POST .../clearance answer (ClearanceOut). */
export interface ClearanceOut {
  id: string;
  checkin_id: string;
  cleared_by: string;
  role: string;
  note: string;
  created_at: string;
}

/** GET /wellness/{player}/state (WellnessStateOut). */
export interface WellnessStateOut {
  as_of: string;
  checked_in: boolean;
  no_checkin: boolean;
  last_checkin_date: string | null;
  days_since_checkin: number | null;
  pain_active: boolean;
  bowling_suppressed: boolean;
  open_pain_checkin_ids: string[];
  escalation: boolean;
  pain_reports_in_window: number;
}

/** GET /players items (cricai_api.routers.players.PlayerOut). The server
 * hides guest players from the player role. */
export interface PlayerOut {
  id: string;
  name: string;
  birthdate: string;
  handedness: "right" | "left";
  is_guest: boolean;
}

export interface CheckinRange {
  start?: string;
  end?: string;
}

export interface WellnessApi {
  listPlayers(): Promise<PlayerOut[]>;
  listCheckins(playerId: string, range?: CheckinRange): Promise<CheckinOut[]>;
  createCheckin(playerId: string, body: CheckinIn): Promise<CheckinOut>;
  clearPain(playerId: string, checkinId: string, note: string): Promise<ClearanceOut>;
  state(playerId: string, asOf?: string): Promise<WellnessStateOut>;
}

export function createWellnessApi(config: ApiConfig = defaultConfig()): WellnessApi {
  return {
    listPlayers: () => apiRequest<PlayerOut[]>("/players", {}, config),
    listCheckins: (playerId, range = {}) =>
      apiRequest<CheckinOut[]>(`/wellness/${playerId}/checkins`, {
        query: { start: range.start, end: range.end },
      }, config),
    createCheckin: (playerId, body) =>
      apiRequest<CheckinOut>(`/wellness/${playerId}/checkins`, { method: "POST", body }, config),
    clearPain: (playerId, checkinId, note) =>
      apiRequest<ClearanceOut>(`/wellness/${playerId}/checkins/${checkinId}/clearance`, {
        method: "POST",
        body: { note },
      }, config),
    state: (playerId, asOf) =>
      apiRequest<WellnessStateOut>(`/wellness/${playerId}/state`, {
        query: { as_of: asOf },
      }, config),
  };
}
