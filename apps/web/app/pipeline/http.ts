/**
 * Shared request helper for the T4 operations screens (pipeline, wellness,
 * alerts, cameras, settings). It rides on the shared `ApiConfig`/`ApiError`
 * from lib/api so the same-origin proxy configuration (`defaultConfig()`)
 * applies unchanged, and it turns any FastAPI `detail` into the one honest
 * line a screen shows verbatim. It lives under app/pipeline because T4 owns no shared folder.
 *
 * Request to T2 (recorded in the T4 status file): fold this into lib/api.ts
 * as an exported `request()` so feature clients share one implementation.
 */

import { ApiError, type ApiConfig } from "@/lib/api";

export type HttpMethod = "GET" | "POST" | "PATCH";

/** Query values; `undefined` entries are omitted from the URL. */
export type Query = Record<string, string | number | boolean | undefined>;

/** Turn a FastAPI `detail` (string, list of problems, or an object carrying a
 * message) into one line. Anything else falls back to the HTTP status text. */
export function detailText(body: unknown, fallback: string): string {
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") {
      return detail;
    }
    if (Array.isArray(detail)) {
      return detail
        .map((item) => (typeof item === "string" ? item : JSON.stringify(item)))
        .join("; ");
    }
    if (typeof detail === "object" && detail !== null) {
      const message = (detail as { message?: unknown }).message;
      const rest = JSON.stringify(detail);
      return typeof message === "string" ? `${message} ${rest}` : rest;
    }
  }
  return fallback;
}

/** Absolute or proxy-relative URL for `path` plus the defined query entries. */
export function buildUrl(baseUrl: string, path: string, query: Query = {}): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined) {
      params.set(key, String(value));
    }
  }
  const search = params.toString();
  return `${baseUrl.replace(/\/$/, "")}${path}${search === "" ? "" : `?${search}`}`;
}

export interface RequestOptions {
  method?: HttpMethod;
  query?: Query;
  body?: unknown;
}

/** JSON request; a non-2xx answer throws `ApiError(status, detail)`. */
export async function request<T>(
  config: ApiConfig,
  path: string,
  { method = "GET", query = {}, body }: RequestOptions = {},
): Promise<T> {
  const fetchFn = config.fetchFn ?? fetch;
  const headers: Record<string, string> = {};
  if (config.token) {
    headers.Authorization = `Bearer ${config.token}`;
  }
  const init: RequestInit = { method, headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const response = await fetchFn(buildUrl(config.baseUrl, path, query), init);
  if (!response.ok) {
    let parsed: unknown;
    try {
      parsed = await response.json();
    } catch {
      parsed = undefined; // non-JSON body: fall through to the status text
    }
    throw new ApiError(response.status, detailText(parsed, response.statusText));
  }
  return (await response.json()) as T;
}
