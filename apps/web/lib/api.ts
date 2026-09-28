/**
 * Typed fetch client for the cricai_api LAN endpoints the dashboard consumes
 * (US-K1, US-B5): sessions, tags, events, clips and ball_metrics routers.
 *
 * Server-data-faithful: every type mirrors the wire schema of its router
 * response model verbatim — the dashboard renders what the API returns and
 * never computes metrics client-side.
 */

// ---------------------------------------------------------------------------
// Enum vocabularies (mirrors of cricai_data.enums / lifecycle — wire values).
// ---------------------------------------------------------------------------

export type Line = "outside_off" | "off" | "middle" | "leg";
export type LengthZone = "yorker" | "full" | "good" | "short";
export type Shot =
  | "leave"
  | "defend"
  | "drive"
  | "cover_drive"
  | "straight_drive"
  | "on_drive"
  | "cut"
  | "pull"
  | "hook"
  | "sweep"
  | "flick"
  | "loft";
export type Footwork = "front" | "back" | "leave";
export type Contact = "middle" | "edge" | "miss";
export type Outcome =
  | "controlled_ground_shot"
  | "controlled_aerial"
  | "uncontrolled"
  | "beaten"
  | "bowled"
  | "edged"
  | "left_alone";
export type SessionType = "batting" | "bowling" | "mixed";
export type BowlerSource = "machine" | "human" | "coach";
export type SessionState =
  | "created"
  | "recording"
  | "captured"
  | "processing"
  | "analyzed"
  | "failed";
export type EventSource = "auto" | "manual" | "corrected";
export type ClipStatus = "pending" | "cut" | "failed" | "gap";
export type MetricPhase = "pre_release" | "contact" | "flight";

// ---------------------------------------------------------------------------
// Response shapes (field-for-field mirrors of the router response models).
// ---------------------------------------------------------------------------

/** GET /sessions items (cricai_api.routers.sessions.SessionOut). */
export interface SessionOut {
  id: string;
  player_id: string;
  session_date: string;
  session_type: SessionType;
  bowler_source: BowlerSource;
  machine_settings: Record<string, unknown> | null;
  notes: string | null;
  state: SessionState;
  degraded: boolean;
  missing_views: string[];
}

export interface SessionPage {
  items: SessionOut[];
  total: number;
  limit: number;
  offset: number;
}

export interface TagAuditOut {
  actor: string;
  field: string;
  old_value: string | null;
  new_value: string | null;
  at: string;
}

/** GET /sessions/{id}/tags items (cricai_api.routers.tags.TagOut). */
export interface TagOut {
  ball_no: number;
  block_no: number | null;
  line: Line;
  length: LengthZone;
  shot: Shot;
  footwork: Footwork;
  contact: Contact;
  outcome: Outcome;
  control: boolean;
  source: string;
  ground_truth_eligible: boolean;
  created_by: string;
  audits: TagAuditOut[];
}

/** GET /sessions/{id}/events items (cricai_api.routers.events.EventOut). */
export interface EventOut {
  id: string;
  session_id: string;
  ball_no: number;
  start_ms: number;
  release_ms: number;
  contact_ms: number | null;
  end_ms: number;
  confidence: number;
  source: EventSource;
  detector_version: string;
  valid: boolean;
  created_at: string;
}

/** GET /sessions/{id}/clips items (cricai_api.routers.clips.ClipOut). */
export interface ClipOut {
  id: string;
  session_id: string;
  ball_no: number;
  camera_id: string;
  object_key: string | null;
  start_ms: number;
  end_ms: number;
  status: ClipStatus;
  error: string | null;
}

/** One metric value under the pinned {value, unit, confidence, reason-if-null,
 * proxy?, source?} contract (US-E2/E3). */
export interface MetricValue {
  value: number | string | boolean | number[] | null;
  unit: string;
  confidence: number;
  reason?: string | null;
  proxy?: boolean;
  source?: string;
}

/** GET /sessions/{id}/balls/{n}/metrics items (ball_metrics.PhaseMetricsOut). */
export interface PhaseMetricsOut {
  phase: MetricPhase;
  metrics: Record<string, MetricValue>;
  schema_version: number;
  stored: boolean;
}

export type VideoStatus = "pending" | "uploaded" | "probed" | "failed" | "metadata_conflict";

/** GET /sessions/{id}/videos items (cricai_api.routers.videos.VideoOut).
 * Parent/coach-only on the server; the player falls back to the assumed
 * capture rate (US-B2) with a visible caption. */
export interface VideoOut {
  id: string;
  session_id: string;
  camera_id: string;
  object_key: string;
  filename: string;
  checksum_sha256: string;
  size_bytes: number;
  claimed_fps: number | null;
  claimed_resolution: string | null;
  claimed_duration_s: number | null;
  codec: string | null;
  status: VideoStatus;
  probe: Record<string, unknown> | null;
  error: string | null;
}

export type ReportKind = "daily" | "weekly" | "monthly";
export type ReportStatus = "draft" | "published" | "blocked";

/** GET /settings/review-queue items (US-J5 coach review queue;
 * cricai_api.routers.settings.ReviewQueueItemOut). */
export interface ReviewQueueItemOut {
  id: string;
  player_id: string;
  session_id: string | null;
  kind: ReportKind;
  period_start: string;
  period_end: string;
  review_due_at: string | null;
  created_at: string | null;
}

/** POST /reports/{id}/publish outcome (US-G3/H5 publish gate;
 * cricai_api.routers.reports.PublishOut). A gate failure answers HTTP 409
 * with this same body: `status: "blocked"` plus the audited reasons list. */
export interface PublishOut {
  status: ReportStatus;
  reasons: string[];
}

// ---------------------------------------------------------------------------
// Client.
// ---------------------------------------------------------------------------

export interface ApiConfig {
  /** Base the paths hang off: the same-origin proxy `/api/cricai` in the
   * browser, or an absolute API origin (server code, tests). */
  baseUrl: string;
  /** Bearer token. Empty in the browser: the proxy attaches the role token
   * from the httpOnly session cookie, so no script ever holds it (US-L3). */
  token: string;
  /** Injectable fetch for tests; defaults to the global fetch. */
  fetchFn?: typeof fetch;
}

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, detail: string) {
    super(`API ${status}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
  }
}

export interface SessionListParams {
  playerId?: string;
  dateFrom?: string;
  dateTo?: string;
  limit?: number;
  offset?: number;
}

export interface ApiClient {
  listSessions(params?: SessionListParams): Promise<SessionPage>;
  getSession(sessionId: string): Promise<SessionOut>;
  listTags(sessionId: string): Promise<TagOut[]>;
  listEvents(sessionId: string): Promise<EventOut[]>;
  listClips(sessionId: string): Promise<ClipOut[]>;
  listSessionVideos(sessionId: string): Promise<VideoOut[]>;
  ballMetrics(sessionId: string, ballNo: number): Promise<PhaseMetricsOut[]>;
}

/** One FastAPI validation problem ({ loc, msg }) as "field: message". */
function problemText(item: unknown): string {
  if (typeof item === "string") {
    return item;
  }
  if (typeof item === "object" && item !== null && typeof (item as { msg?: unknown }).msg === "string") {
    const { loc, msg } = item as { loc?: unknown; msg: string };
    // Drop the request-part prefix ("body", "query", ...): the field path is what matters.
    const field = Array.isArray(loc) ? loc.slice(1).join(".") : "";
    return field ? `${field}: ${msg}` : msg;
  }
  return JSON.stringify(item);
}

/**
 * A FastAPI `detail` as one honest line, the server's words kept verbatim:
 * a string as is; a list of problems as "field: message; ..."; an object as
 * its `message` followed by "key: a, b" for each other scalar or list entry
 * (e.g. "Unknown cameras — unknown_cameras: C7, C9"). Anything else, or no
 * readable detail, falls back to the HTTP status text.
 */
export function detailText(body: unknown, fallback: string): string {
  if (typeof body !== "object" || body === null || !("detail" in body)) {
    return fallback;
  }
  const detail = (body as { detail: unknown }).detail;
  if (typeof detail === "string") {
    return detail;
  }
  if (Array.isArray(detail)) {
    return detail.length > 0 ? detail.map(problemText).join("; ") : fallback;
  }
  if (typeof detail === "object" && detail !== null) {
    const parts: string[] = [];
    for (const [key, value] of Object.entries(detail)) {
      if (key === "message" && typeof value === "string") {
        parts.unshift(value);
      } else if (Array.isArray(value) && value.length > 0) {
        parts.push(`${key}: ${value.map(String).join(", ")}`);
      } else if (["string", "number", "boolean"].includes(typeof value)) {
        parts.push(`${key}: ${String(value)}`);
      }
    }
    return parts.length > 0 ? parts.join(" — ") : fallback;
  }
  return fallback;
}

async function errorDetail(response: Response): Promise<string> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = undefined; // non-JSON body: fall through to the status text
  }
  return detailText(body, response.statusText);
}

/** Authorization header for a config's token; none when the token is empty (proxy mode). */
function bearer(config: ApiConfig): Record<string, string> {
  return config.token ? { Authorization: `Bearer ${config.token}` } : {};
}

/** Query values; undefined entries are left out of the URL. */
export type ApiQuery = Record<string, string | number | boolean | undefined>;

/** `base + path + ?query`, without undefined params. Works for relative (proxy) bases. */
function buildUrl(config: ApiConfig, path: string, query: ApiQuery): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined) {
      params.set(key, String(value));
    }
  }
  const search = params.toString();
  return config.baseUrl.replace(/\/$/, "") + path + (search ? `?${search}` : "");
}

export interface ApiRequestInit {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  query?: ApiQuery;
  /** JSON-serialised request body. */
  body?: unknown;
}

/**
 * One JSON call through the shared convention: base URL, auth, query
 * building, ApiError with the server's detail on non-2xx; a 204 resolves to
 * undefined. Feature clients should build on this instead of raw fetch.
 */
export async function apiRequest<T>(
  path: string,
  init: ApiRequestInit = {},
  config: ApiConfig = defaultConfig(),
): Promise<T> {
  const { method = "GET", query = {}, body } = init;
  const headers: Record<string, string> = { Accept: "application/json", ...bearer(config) };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  const fetchFn = config.fetchFn ?? fetch;
  const response = await fetchFn(buildUrl(config, path, query), {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    throw new ApiError(response.status, await errorDetail(response));
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

function request<T>(
  config: ApiConfig,
  path: string,
  query: Record<string, string | number | undefined> = {},
): Promise<T> {
  return apiRequest<T>(path, { query }, config);
}

export function createApiClient(config: ApiConfig): ApiClient {
  return {
    listSessions: (params = {}) =>
      request<SessionPage>(config, "/sessions", {
        player_id: params.playerId,
        date_from: params.dateFrom,
        date_to: params.dateTo,
        limit: params.limit,
        offset: params.offset,
      }),
    getSession: (sessionId) => request<SessionOut>(config, `/sessions/${sessionId}`),
    listTags: (sessionId) => request<TagOut[]>(config, `/sessions/${sessionId}/tags`),
    listEvents: (sessionId) => request<EventOut[]>(config, `/sessions/${sessionId}/events`),
    listClips: (sessionId) => request<ClipOut[]>(config, `/sessions/${sessionId}/clips`),
    listSessionVideos: (sessionId) =>
      request<VideoOut[]>(config, `/sessions/${sessionId}/videos`),
    ballMetrics: (sessionId, ballNo) =>
      request<PhaseMetricsOut[]>(config, `/sessions/${sessionId}/balls/${ballNo}/metrics`),
  };
}

// ---------------------------------------------------------------------------
// The single web API configuration convention (US-K1..K5, US-L3).
//
// The browser only talks to its own origin: every dashboard feature calls the
// same-origin proxy /api/cricai/*, whose route handler forwards to cricai_api
// (server-only CRICAI_API_BASE_URL) with the role token from the httpOnly
// cricai_session cookie set by /login. No token is in the bundle, the API
// needs no CORS, and one build serves every role (apps/web/README.md).
// ---------------------------------------------------------------------------

/** Same-origin path of the API proxy (app/api/cricai/[...path]/route.ts). */
export const API_PROXY_BASE = "/api/cricai";

/** Base for every API path: the same-origin proxy. */
export function apiBase(): string {
  return API_PROXY_BASE;
}

/** Always empty: the browser never holds the role token (the proxy adds it). */
export function apiToken(): string {
  return "";
}

/** No Authorization header from the browser; kept so feature clients compile unchanged. */
export function authHeaders(): Record<string, string> {
  return {};
}

/** Config for the same-origin proxy: no bearer. */
export function defaultConfig(): ApiConfig {
  return { baseUrl: apiBase(), token: apiToken() };
}

// ---------------------------------------------------------------------------
// Clip media resolution.
// ---------------------------------------------------------------------------

/**
 * Resolve a clip's object_key against the LAN object-store base URL.
 *
 * cricai_api exposes clip rows (object keys), not a media-streaming endpoint;
 * the store itself serves bytes on the LAN (US-B5: no cloud dependency).
 */
export function clipMediaUrl(mediaBase: string, objectKey: string): string {
  const base = mediaBase.endsWith("/") ? mediaBase.slice(0, -1) : mediaBase;
  const key = objectKey.startsWith("/") ? objectKey.slice(1) : objectKey;
  return `${base}/${key}`;
}

/** Object-store base from the NEXT_PUBLIC_* build-time environment. */
export function defaultMediaBase(): string {
  return process.env.NEXT_PUBLIC_CRICAI_MEDIA_BASE ?? "http://localhost:9000/cricai";
}

// ---------------------------------------------------------------------------
// US-J5 review queue + US-G3/H5 publish gate (coach surfaces).
// ---------------------------------------------------------------------------

/** GET /settings/review-queue: draft reports held by the review gate,
 * earliest deadline first (US-J5). Coach token only — the server answers
 * other roles with 403, which surfaces here as an ApiError. */
export function listReviewQueue(config: ApiConfig = defaultConfig()): Promise<ReviewQueueItemOut[]> {
  return request<ReviewQueueItemOut[]>(config, "/settings/review-queue");
}

/** Wire statuses a publish decision may carry (ReportStatus mirror). */
const PUBLISH_STATUSES: ReadonlySet<string> = new Set(["draft", "published", "blocked"]);

/** True only for a genuine PublishOut decision body (status + string reasons). */
function isPublishOut(body: unknown): body is PublishOut {
  if (typeof body !== "object" || body === null) {
    return false;
  }
  const candidate = body as { status?: unknown; reasons?: unknown };
  return (
    typeof candidate.status === "string" &&
    PUBLISH_STATUSES.has(candidate.status) &&
    Array.isArray(candidate.reasons) &&
    candidate.reasons.every((reason: unknown) => typeof reason === "string")
  );
}

/** POST /reports/{id}/publish: run the US-G3/G6/H5 publish gate.
 *
 * Resolves with the DECIDED outcome, blocked included: the gate answers HTTP
 * 409 with the same PublishOut body carrying the audited reasons list, so a
 * blocked report is a decision to display, never a transport error. Any other
 * non-2xx throws ApiError — as does a 2xx/409 whose body is not a PublishOut
 * decision (e.g. an intermediary proxy's own 409), so the caller renders an
 * honest failure instead of silence. */
export async function publishReport(
  reportId: string,
  config: ApiConfig = defaultConfig(),
): Promise<PublishOut> {
  const fetchFn = config.fetchFn ?? fetch;
  const url = `${config.baseUrl.replace(/\/$/, "")}/reports/${reportId}/publish`;
  const response = await fetchFn(url, { method: "POST", headers: bearer(config) });
  if (!response.ok && response.status !== 409) {
    throw new ApiError(response.status, await errorDetail(response));
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = undefined; // non-JSON body: fall through to the shape check
  }
  if (!isPublishOut(body)) {
    throw new ApiError(
      response.status,
      `publish returned no readable decision (HTTP ${response.status})`,
    );
  }
  return body;
}
