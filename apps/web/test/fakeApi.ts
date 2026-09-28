/**
 * In-memory fake of the cricai_api surface (T1, phase-8-team.md §3.4).
 *
 * Serves BOTH ways the dashboard talks to the API:
 *  - as an `ApiClient` to inject into components that take a `client` prop;
 *  - as a `fetch` stub (`api.fetch`) for feature api modules that call the
 *    global fetch directly — install it with `vi.stubGlobal("fetch", api.fetch)`.
 *
 * Controls, keyed by a client method name ("listSessions") or, for direct
 * fetches, by a URL path ("/reports?player_id=p1&kind=weekly" or "/reports"):
 *  - `failWith(key, status)`  → the client throws ApiError / fetch answers status
 *  - `respondWith(key, body)` → override the body served for that key
 *  - `hold(key)`              → the call stays pending until the returned release()
 *  - `calls`                  → every call, in order, with its arguments
 *  - `reset()`                → clear controls and calls, keep the seed
 */

import { ApiError } from "@/lib/api";
import type {
  ApiClient,
  ClipOut,
  EventOut,
  PhaseMetricsOut,
  ReviewQueueItemOut,
  SessionOut,
  TagOut,
  VideoOut,
} from "@/lib/api";
import { sessionPage } from "./fixtures";

export interface FakeSeed {
  sessions: SessionOut[];
  /** session id → rows */
  tags: Record<string, TagOut[]>;
  events: Record<string, EventOut[]>;
  clips: Record<string, ClipOut[]>;
  videos: Record<string, VideoOut[]>;
  /** `${sessionId}:${ballNo}` → phases */
  ballMetrics: Record<string, PhaseMetricsOut[]>;
  reviewQueue: ReviewQueueItemOut[];
  /** Extra GET routes for direct-fetch modules: path (optionally with query) → body. */
  routes: Record<string, unknown>;
}

export type FakeMethod = keyof ApiClient | (string & {});

export interface RecordedCall {
  method: string;
  args: unknown[];
}

interface Failure {
  status: number;
  detail: string;
}

export interface FakeApi extends ApiClient {
  readonly seed: FakeSeed;
  readonly calls: RecordedCall[];
  failWith(method: FakeMethod, status: number, detail?: string): void;
  respondWith(method: FakeMethod, body: unknown): void;
  hold(method: FakeMethod): () => void;
  reset(): void;
  fetch: typeof fetch;
}

export function emptySeed(): FakeSeed {
  return {
    sessions: [],
    tags: {},
    events: {},
    clips: {},
    videos: {},
    ballMetrics: {},
    reviewQueue: [],
    routes: {},
  };
}

function jsonResponse(status: number, body: unknown): Response {
  const ok = status >= 200 && status < 300;
  const shaped = {
    ok,
    status,
    statusText: ok ? "OK" : `HTTP ${status}`,
    headers: new Headers({ "content-type": "application/json" }),
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  };
  return shaped as unknown as Response;
}

interface Resolved {
  /** Control key: the client method for known routes, else the path. */
  key: string;
  body: () => unknown;
  args: unknown[];
}

function notFound(): never {
  throw new ApiError(404, "not found");
}

const SESSION_CHILD = /^\/sessions\/([^/]+)\/(tags|events|clips|videos)$/;
const BALL_METRICS = /^\/sessions\/([^/]+)\/balls\/(\d+)\/metrics$/;
const SESSION_ONE = /^\/sessions\/([^/]+)$/;

export function createFakeApi(seed: Partial<FakeSeed> = {}): FakeApi {
  const data: FakeSeed = { ...emptySeed(), ...seed };
  const calls: RecordedCall[] = [];
  const failures = new Map<string, Failure>();
  const overrides = new Map<string, unknown>();
  const holds = new Map<string, Promise<void>>();

  function getSession(sessionId: string): SessionOut {
    return data.sessions.find((row) => row.id === sessionId) ?? notFound();
  }

  async function gate(key: string, args: unknown[]): Promise<Failure | null> {
    calls.push({ method: key, args });
    const pending = holds.get(key);
    if (pending) {
      await pending;
    }
    return failures.get(key) ?? null;
  }

  async function dispatch<T>(key: string, args: unknown[], produce: () => T): Promise<T> {
    const failure = await gate(key, args);
    if (failure) {
      throw new ApiError(failure.status, failure.detail);
    }
    if (overrides.has(key)) {
      return overrides.get(key) as T;
    }
    return produce();
  }

  function resolve(pathname: string, search: string): Resolved {
    const query = Object.fromEntries(new URLSearchParams(search));
    if (pathname === "/sessions") {
      return { key: "listSessions", args: [query], body: () => sessionPage(data.sessions) };
    }
    if (pathname === "/settings/review-queue") {
      return { key: "listReviewQueue", args: [], body: () => data.reviewQueue };
    }
    const metrics = BALL_METRICS.exec(pathname);
    if (metrics) {
      const [, sessionId, ballNo] = metrics;
      return {
        key: "ballMetrics",
        args: [sessionId, Number(ballNo)],
        body: () => data.ballMetrics[`${sessionId}:${ballNo}`] ?? [],
      };
    }
    const child = SESSION_CHILD.exec(pathname);
    if (child) {
      const [, sessionId, kind] = child;
      const method = {
        tags: "listTags",
        events: "listEvents",
        clips: "listClips",
        videos: "listSessionVideos",
      }[kind as "tags" | "events" | "clips" | "videos"];
      return {
        key: method,
        args: [sessionId],
        body: () => data[kind as "tags" | "events" | "clips" | "videos"][sessionId] ?? [],
      };
    }
    const one = SESSION_ONE.exec(pathname);
    if (one) {
      return { key: "getSession", args: [one[1]], body: () => getSession(one[1]) };
    }
    const withQuery = pathname + search;
    const key = withQuery in data.routes || overrides.has(withQuery) ? withQuery : pathname;
    return {
      key,
      args: [query],
      body: () => {
        if (withQuery in data.routes) {
          return data.routes[withQuery];
        }
        if (pathname in data.routes) {
          return data.routes[pathname];
        }
        return notFound();
      },
    };
  }

  const fakeFetch: typeof fetch = async (input, init) => {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(raw, "http://fake.local");
    const resolved = resolve(url.pathname, url.search);
    const failure = await gate(resolved.key, [...resolved.args, init?.method ?? "GET"]);
    if (failure) {
      return jsonResponse(failure.status, { detail: failure.detail });
    }
    if (overrides.has(resolved.key)) {
      return jsonResponse(200, overrides.get(resolved.key));
    }
    try {
      return jsonResponse(200, resolved.body());
    } catch (error) {
      const status = error instanceof ApiError ? error.status : 500;
      return jsonResponse(status, { detail: error instanceof Error ? error.message : "error" });
    }
  };

  return {
    seed: data,
    calls,
    fetch: fakeFetch,
    failWith(method, status, detail = `fake ${status}`) {
      failures.set(method, { status, detail });
    },
    respondWith(method, body) {
      overrides.set(method, body);
    },
    hold(method) {
      let release: () => void = () => undefined;
      const pending = new Promise<void>((resolveHold) => {
        release = resolveHold;
      });
      holds.set(method, pending);
      return () => {
        holds.delete(method);
        release();
      };
    },
    reset() {
      failures.clear();
      overrides.clear();
      holds.clear();
      calls.length = 0;
    },
    listSessions: (params = {}) =>
      dispatch("listSessions", [params], () => sessionPage(data.sessions)),
    getSession: (sessionId) => dispatch("getSession", [sessionId], () => getSession(sessionId)),
    listTags: (sessionId) => dispatch("listTags", [sessionId], () => data.tags[sessionId] ?? []),
    listEvents: (sessionId) =>
      dispatch("listEvents", [sessionId], () => data.events[sessionId] ?? []),
    listClips: (sessionId) =>
      dispatch("listClips", [sessionId], () => data.clips[sessionId] ?? []),
    listSessionVideos: (sessionId) =>
      dispatch("listSessionVideos", [sessionId], () => data.videos[sessionId] ?? []),
    ballMetrics: (sessionId, ballNo) =>
      dispatch(
        "ballMetrics",
        [sessionId, ballNo],
        () => data.ballMetrics[`${sessionId}:${ballNo}`] ?? [],
      ),
  };
}
