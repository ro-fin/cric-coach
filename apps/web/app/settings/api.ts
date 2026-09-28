/**
 * Settings feature client (US-J5/L5). Types mirror
 * `cricai_api.routers.settings` field for field. Versions are append-only:
 * a change is a new version with a reason; the server enforces who may
 * approve which change and answers 403 with the missing approval named.
 */

import { apiRequest, defaultConfig, type ApiConfig } from "@/lib/api";

/** cricai_data.enums.ReviewMode. */
export type ReviewMode = "auto_publish" | "coach_gate";

export const REVIEW_MODES: readonly ReviewMode[] = ["auto_publish", "coach_gate"];

/** The two required sections of a settings version (REQUIRED_SECTIONS). */
export interface AppSettings {
  report_review: { mode: ReviewMode; timeout_hours: number } & Record<string, unknown>;
  live_mode: { enabled: boolean; allowlist: string[] } & Record<string, unknown>;
}

/** GET /settings, GET /settings/versions items, POST /settings answer
 * (AppSettingsOut). `created_at` is null for the unseeded version 0. */
export interface AppSettingsOut {
  version: number;
  settings: AppSettings;
  approved_by: string;
  reason: string;
  created_at: string | null;
}

/** POST /settings body (AppSettingsIn). */
export interface AppSettingsIn {
  settings: AppSettings;
  reason: string;
}

export interface SettingsApi {
  active(): Promise<AppSettingsOut>;
  versions(): Promise<AppSettingsOut[]>;
  createVersion(body: AppSettingsIn): Promise<AppSettingsOut>;
}

export function createSettingsApi(config: ApiConfig = defaultConfig()): SettingsApi {
  return {
    active: () => apiRequest<AppSettingsOut>("/settings", {}, config),
    versions: () => apiRequest<AppSettingsOut[]>("/settings/versions", {}, config),
    createVersion: (body) =>
      apiRequest<AppSettingsOut>("/settings", { method: "POST", body }, config),
  };
}
