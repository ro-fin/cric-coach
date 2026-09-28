/**
 * Alerts feature client (US-L4 dashboard half). Types mirror
 * `cricai_api.routers.alerts` field for field. Routing is authorization on
 * the server: parents see parent-audience alerts, the coach (developer seat)
 * sees developer-audience alerts, and players get 403.
 */

import { apiRequest, defaultConfig, type ApiConfig } from "@/lib/api";

/** cricai_data.enums.AlertAudience. */
export type AlertAudience = "parent" | "developer";

/** GET /alerts items and POST /alerts/{id}/ack answer (AlertOut). */
export interface AlertOut {
  id: string;
  audience: AlertAudience;
  code: string;
  severity: string;
  detail: Record<string, unknown>;
  session_id: string | null;
  acknowledged: boolean;
  created_at: string;
}

export interface AlertFilter {
  audience?: AlertAudience;
  acknowledged?: boolean;
}

export interface AlertsApi {
  listAlerts(filter?: AlertFilter): Promise<AlertOut[]>;
  acknowledge(alertId: string): Promise<AlertOut>;
}

export function createAlertsApi(config: ApiConfig = defaultConfig()): AlertsApi {
  return {
    listAlerts: (filter = {}) =>
      apiRequest<AlertOut[]>("/alerts", {
        query: { audience: filter.audience, acknowledged: filter.acknowledged },
      }, config),
    acknowledge: (alertId) =>
      apiRequest<AlertOut>(`/alerts/${alertId}/ack`, { method: "POST" }, config),
  };
}
