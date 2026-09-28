/**
 * Pitch-map fetch helpers (US-K2). Base URL and bearer token come from the
 * single lib/api convention (NEXT_PUBLIC_API_BASE_URL + NEXT_PUBLIC_API_TOKEN;
 * see apps/web/README.md) so one deployment config serves every feature.
 * Response shapes mirror the API routers exactly: the view renders ONLY what
 * the server returns (US-K4 data parity).
 */

import { apiBase, authHeaders } from "@/lib/api";

export interface SourceCounts {
  manual: number;
  auto: number;
}

/** One line x length cell of `GET /sessions/{id}/heatmap` (US-C6/US-F4). */
export interface HeatmapCell {
  line: string;
  length: string;
  balls: number;
  control_pct: number | null;
  false_shot_pct: number | null;
  sources: SourceCounts;
  hollow: number;
}

/** One effective bounce point with provenance (US-F4). */
export interface BouncePoint {
  ball_no: number;
  line: string;
  length: string;
  pitch_x: number;
  pitch_y: number;
  source: string;
  confidence: number | null;
  hollow: boolean;
}

export interface SessionHeatmap {
  session_id: string;
  total_balls: number;
  cells: HeatmapCell[];
  flagged_balls: number[];
  points: BouncePoint[];
}

export interface SessionInfo {
  id: string;
  player_id: string;
  session_date: string;
}

export interface PlayerInfo {
  id: string;
  name: string;
  handedness: "right" | "left";
  is_guest: boolean;
}

/** Ball identity from `GET /sessions/{id}/tags`: the honest full ball list —
 * a tagged ball with no bounce point (beamer/full toss) still exists. */
export interface TaggedBall {
  ball_no: number;
}

/** Declared bowling target (US-I4), from the story-i1 targets router. */
export interface BowlingTarget {
  line: string;
  length: string;
  description: string;
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${apiBase()}${path}`, { headers: authHeaders() });
  if (!response.ok) {
    throw new Error(`GET ${path} failed: ${response.status}`);
  }
  return (await response.json()) as T;
}

export function fetchSessionHeatmap(sessionId: string): Promise<SessionHeatmap> {
  return getJson(`/sessions/${sessionId}/heatmap`);
}

export function fetchSession(sessionId: string): Promise<SessionInfo> {
  return getJson(`/sessions/${sessionId}`);
}

export function fetchPlayer(playerId: string): Promise<PlayerInfo> {
  return getJson(`/players/${playerId}`);
}

export function fetchTags(sessionId: string): Promise<TaggedBall[]> {
  return getJson(`/sessions/${sessionId}/tags`);
}

/** Declared targets for the session, or null when the targets API cannot
 * serve them (endpoint ships with story i1) — absence renders honestly as
 * "targets unavailable", never as an empty overlay. */
export async function fetchTargets(sessionId: string): Promise<BowlingTarget[] | null> {
  try {
    return await getJson(`/targets?session_id=${sessionId}`);
  } catch {
    return null;
  }
}
