/**
 * Progress-feature fetch helpers (US-K4). Base URL and token come from the
 * shared `defaultConfig()` in lib/api (the same-origin proxy once F2b lands,
 * where the token is empty and no Authorization header is sent).
 *
 * Consumes only the pinned API surface (contract #4): reports, milestones,
 * workload summary, wellness state. Responses pass through UNCHANGED — the
 * dashboard renders exactly what the API returned (US-K4 data-parity).
 */

import { defaultConfig } from "@/lib/api";
import type {
  DashboardData,
  Milestone,
  RollupReport,
  WellnessState,
  WorkloadWindow,
} from "./types";

export type ApiConfig = {
  base: string;
  token: string;
};

const SHARED = defaultConfig();

/** The shared lib/api defaults; override per call for tests and multi-lab setups. */
export const DEFAULT_CONFIG: ApiConfig = {
  base: SHARED.baseUrl,
  token: SHARED.token,
};

async function getJson<T>(config: ApiConfig, path: string): Promise<T> {
  const response = await fetch(`${config.base}${path}`, {
    headers: config.token ? { Authorization: `Bearer ${config.token}` } : {},
  });
  if (!response.ok) {
    throw new Error(`GET ${path} failed: ${response.status}`);
  }
  return (await response.json()) as T;
}

export function fetchRollupReports(
  config: ApiConfig,
  playerId: string,
  kind: "weekly" | "monthly",
): Promise<RollupReport[]> {
  return getJson(config, `/reports?player_id=${playerId}&kind=${kind}`);
}

export function fetchMilestones(config: ApiConfig, playerId: string): Promise<Milestone[]> {
  return getJson(config, `/milestones/players/${playerId}`);
}

export function fetchWorkloadSummary(
  config: ApiConfig,
  playerId: string,
): Promise<WorkloadWindow[]> {
  return getJson(config, `/workload/players/${playerId}/summary`);
}

export function fetchWellnessState(config: ApiConfig, playerId: string): Promise<WellnessState> {
  return getJson(config, `/wellness/${playerId}/state`);
}

/** The newest report of a kind — the reports API lists newest period first. */
export function newestReport(reports: RollupReport[]): RollupReport | null {
  return reports.length > 0 ? reports[0] : null;
}

/** Load everything the dashboard shows in one pass (US-K4). */
export async function loadDashboard(
  config: ApiConfig,
  playerId: string,
): Promise<DashboardData> {
  const [weekly, monthly, milestones, workload, wellness] = await Promise.all([
    fetchRollupReports(config, playerId, "weekly"),
    fetchRollupReports(config, playerId, "monthly"),
    fetchMilestones(config, playerId),
    fetchWorkloadSummary(config, playerId),
    fetchWellnessState(config, playerId),
  ]);
  return {
    weekly: newestReport(weekly),
    monthly: newestReport(monthly),
    milestones,
    workload: workload.length > 0 ? workload[0] : null,
    wellness,
  };
}
