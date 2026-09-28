/**
 * Today (home) data client: the three router surfaces the home screen reads.
 *
 * Field-for-field mirrors of the router response models, pinned from source:
 * - `GET /players` -> list[PlayerOut] (routers/players.py; a player token never
 *   sees guest players, US-L3);
 * - `GET /reports?player_id&kind=daily` -> list[ReportOut] (routers/reports.py;
 *   newest period first; a player token sees only PUBLISHED reports, US-G3/H5);
 * - `GET /workload/players/{id}/summary` -> list[WindowSummaryOut]
 *   (routers/workload.py; rolling-7 windows ending today, newest first, US-H1).
 *
 * Every value is rendered as received — nothing is computed here (US-K4).
 * The base URL and auth come from the injectable lib/api `ApiConfig`, so the
 * same-origin proxy switch (Phase 8 F2) needs no change in this module.
 */

import { ApiError, defaultConfig } from "@/lib/api";
import type { ApiConfig, ReportKind, ReportStatus } from "@/lib/api";
import type { ReportBody } from "@/app/reports/api";

export type Handedness = "right" | "left";

/** routers/players.py PlayerOut. */
export interface PlayerOut {
  id: string;
  name: string;
  birthdate: string;
  handedness: Handedness;
  is_guest: boolean;
}

/** routers/reports.py ReportOut. */
export interface ReportOut {
  id: string;
  player_id: string;
  session_id: string | null;
  kind: ReportKind;
  period_start: string;
  period_end: string;
  status: ReportStatus;
  body: ReportBody;
  quality: Record<string, unknown> | null;
  created_at: string;
}

/** cricai_data.enums.SafetyCode wire values. */
export type SafetyCode = "workload_ceiling" | "day_pattern_violation" | "pain_flag";

/** routers/workload.py WindowSummaryOut. */
export interface WindowSummaryOut {
  window_start: string;
  window_end: string;
  weighted_balls: number;
  weighted_overs: number;
  bowling_days: string[];
  consecutive_day_pairs: number;
  band_max_age: number | null;
  ceiling_overs: number | null;
  violations: SafetyCode[];
  remaining_balls: number | null;
}

export interface TodayApi {
  listPlayers(): Promise<PlayerOut[]>;
  listDailyReports(playerId: string): Promise<ReportOut[]>;
  workloadSummary(playerId: string): Promise<WindowSummaryOut[]>;
}

async function getJson<T>(config: ApiConfig, path: string): Promise<T> {
  const fetchFn = config.fetchFn ?? fetch;
  const headers: Record<string, string> = config.token
    ? { Authorization: `Bearer ${config.token}` }
    : {};
  const response = await fetchFn(`${config.baseUrl.replace(/\/$/, "")}${path}`, { headers });
  if (!response.ok) {
    throw new ApiError(response.status, response.statusText);
  }
  return (await response.json()) as T;
}

export function createTodayApi(config: ApiConfig = defaultConfig()): TodayApi {
  return {
    listPlayers: () => getJson<PlayerOut[]>(config, "/players"),
    listDailyReports: (playerId) =>
      getJson<ReportOut[]>(
        config,
        `/reports?${new URLSearchParams({ player_id: playerId, kind: "daily" })}`,
      ),
    workloadSummary: (playerId) =>
      getJson<WindowSummaryOut[]>(
        config,
        `/workload/players/${encodeURIComponent(playerId)}/summary`,
      ),
  };
}
