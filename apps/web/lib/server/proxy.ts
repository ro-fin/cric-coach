/**
 * Server-side API proxy (BFF) behind /api/cricai/* — server code only.
 *
 * The browser calls its own origin; this forwards to cricai_api at the
 * server-only CRICAI_API_BASE_URL with the bearer taken from the httpOnly
 * cricai_session cookie, never from the browser. Only allow-listed headers
 * cross in either direction. An upstream 401 clears the cookie so the next
 * page load goes back to /login.
 *
 * /api/cricai/_session is the proxy's own endpoint: GET -> { role },
 * POST { role, token } -> verify against the API and set the cookie,
 * DELETE -> sign out.
 */
import { isRole, type Role } from "@/lib/auth/roles";
import {
  decodeSession,
  encodeSession,
  SESSION_COOKIE,
  sessionCookieOptions,
  type SessionData,
} from "@/lib/auth/session";

/** Request headers the browser may pass through to the API. Everything else (Authorization, Cookie, ...) is dropped. */
export const FORWARDED_REQUEST_HEADERS = [
  "accept",
  "accept-language",
  "content-type",
  "if-match",
  "if-modified-since",
  "if-none-match",
  "range",
] as const;

/** Response headers passed back. Length and encoding are left to the runtime (fetch already decoded the body). */
export const FORWARDED_RESPONSE_HEADERS = [
  "accept-ranges",
  "cache-control",
  "content-disposition",
  "content-range",
  "content-type",
  "etag",
  "last-modified",
] as const;

export interface ProxyDeps {
  fetchFn?: typeof fetch;
  /** Defaults to process.env.CRICAI_API_BASE_URL. */
  apiBaseUrl?: string;
}

export const DEFAULT_API_BASE_URL = "http://localhost:8000";

function apiBase(deps: ProxyDeps): string {
  const base = deps.apiBaseUrl ?? process.env.CRICAI_API_BASE_URL ?? DEFAULT_API_BASE_URL;
  return base.replace(/\/+$/, "");
}

function json(status: number, body: unknown, headers: Headers = new Headers()): Response {
  headers.set("content-type", "application/json");
  headers.set("cache-control", "no-store");
  return new Response(JSON.stringify(body), { status, headers });
}

function cookieHeader(value: string, secure: boolean, maxAge?: number): string {
  const options = sessionCookieOptions(secure, maxAge);
  return [
    `${SESSION_COOKIE}=${value}`,
    `Path=${options.path}`,
    `Max-Age=${options.maxAge}`,
    "HttpOnly",
    "SameSite=Strict",
    ...(options.secure ? ["Secure"] : []),
  ].join("; ");
}

function isSecure(request: Request): boolean {
  return new URL(request.url).protocol === "https:";
}

function clearCookie(request: Request, headers: Headers = new Headers()): Headers {
  headers.append("set-cookie", cookieHeader("", isSecure(request), 0));
  return headers;
}

/** The raw cricai_session value from the Cookie header (the browser's own cookies stay here). */
export function readSessionCookie(request: Request): string | undefined {
  const header = request.headers.get("cookie") ?? "";
  for (const part of header.split(";")) {
    const [name, ...rest] = part.trim().split("=");
    if (name === SESSION_COOKIE) {
      return rest.join("=");
    }
  }
  return undefined;
}

/** State-changing calls must come from this origin (SameSite=Strict is the first line; this is the second). */
function crossOrigin(request: Request): boolean {
  const origin = request.headers.get("origin");
  return origin !== null && origin !== new URL(request.url).origin;
}

/** Path segments must be plain names: no empty, dot or reserved (_) segments. */
function safePath(path: string[]): string | null {
  if (path.length === 0 || path.some((part) => part === "" || part === "." || part === ".." || part.startsWith("_"))) {
    return null;
  }
  return path.map(encodeURIComponent).join("/");
}

type Probe = Role | "invalid" | "unavailable";

/**
 * Which role a token belongs to. The API has no "who am I", so two read-only,
 * role-exclusive endpoints tell the roles apart: parent-only GET
 * /privacy/audit and coach-only GET /settings/review-queue.
 */
async function probeRole(token: string, deps: ProxyDeps): Promise<Probe> {
  const fetchFn = deps.fetchFn ?? fetch;
  const status = (path: string) =>
    fetchFn(`${apiBase(deps)}${path}`, {
      headers: { Authorization: `Bearer ${token}`, accept: "application/json" },
      cache: "no-store",
    }).then((response) => response.status);
  let audit: number;
  let queue: number;
  try {
    [audit, queue] = await Promise.all([status("/privacy/audit"), status("/settings/review-queue")]);
  } catch {
    return "unavailable";
  }
  if (audit === 401 || queue === 401) {
    return "invalid";
  }
  if (audit === 200) {
    return "parent";
  }
  if (queue === 200) {
    return "coach";
  }
  return audit === 403 && queue === 403 ? "player" : "unavailable";
}

async function signIn(request: Request, deps: ProxyDeps): Promise<Response> {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    body = null;
  }
  const { role, token } = (body ?? {}) as { role?: unknown; token?: unknown };
  if (!isRole(role) || typeof token !== "string" || token.trim() === "") {
    return json(400, { detail: "Choose a role and enter its token." });
  }
  const actual = await probeRole(token.trim(), deps);
  if (actual === "unavailable") {
    return json(502, { detail: "The cricAI API could not be reached. Try again in a moment." });
  }
  if (actual === "invalid") {
    return json(401, { detail: "That token was not accepted." });
  }
  if (actual !== role) {
    return json(403, { detail: `That token is not a ${role} token.` });
  }
  const session: SessionData = { role, token: token.trim() };
  const headers = new Headers();
  headers.append("set-cookie", cookieHeader(encodeSession(session), isSecure(request)));
  return json(200, { role }, headers);
}

async function sessionEndpoint(request: Request, deps: ProxyDeps): Promise<Response> {
  switch (request.method) {
    case "GET": {
      const session = decodeSession(readSessionCookie(request));
      return json(200, { role: session?.role ?? null });
    }
    case "POST":
      return signIn(request, deps);
    case "DELETE":
      return new Response(null, { status: 204, headers: clearCookie(request) });
    default:
      return json(405, { detail: "Method not allowed" }, new Headers({ allow: "GET, POST, DELETE" }));
  }
}

export async function proxy(request: Request, path: string[], deps: ProxyDeps = {}): Promise<Response> {
  if (request.method !== "GET" && request.method !== "HEAD" && crossOrigin(request)) {
    return json(403, { detail: "Cross-origin request refused" });
  }
  if (path.length === 1 && path[0] === "_session") {
    return sessionEndpoint(request, deps);
  }
  const target = safePath(path);
  if (target === null) {
    return json(404, { detail: "Not found" });
  }
  const raw = readSessionCookie(request);
  const session = decodeSession(raw);
  if (session === null) {
    return json(401, { detail: "Not signed in" }, raw === undefined ? undefined : clearCookie(request));
  }

  const headers = new Headers();
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value !== null) {
      headers.set(name, value);
    }
  }
  headers.set("authorization", `Bearer ${session.token}`);

  const hasBody = request.method !== "GET" && request.method !== "HEAD";
  const fetchFn = deps.fetchFn ?? fetch;
  let upstream: Response;
  try {
    upstream = await fetchFn(`${apiBase(deps)}/${target}${new URL(request.url).search}`, {
      method: request.method,
      headers,
      body: hasBody ? await request.arrayBuffer() : undefined,
      cache: "no-store",
      redirect: "manual",
    });
  } catch {
    return json(502, { detail: "The cricAI API could not be reached." });
  }

  const out = new Headers();
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value !== null) {
      out.set(name, value);
    }
  }
  if (upstream.status === 401) {
    clearCookie(request, out);
  }
  const empty = upstream.status === 204 || upstream.status === 304 || request.method === "HEAD";
  return new Response(empty ? null : upstream.body, { status: upstream.status, headers: out });
}
