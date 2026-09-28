import { afterEach, describe, expect, it, vi } from "vitest";
import {
  API_PROXY_BASE,
  ApiError,
  apiBase,
  apiRequest,
  apiToken,
  authHeaders,
  clipMediaUrl,
  createApiClient,
  defaultConfig,
  defaultMediaBase,
  listReviewQueue,
  publishReport,
  ReviewQueueItemOut,
} from "./api";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function fakeFetch(response: Response) {
  return vi.fn(async () => response) as unknown as typeof fetch;
}

const CONFIG = { baseUrl: "http://lab:8000", token: "tok" };

describe("createApiClient", () => {
  it("lists sessions with query params and auth header", async () => {
    const page = { items: [], total: 0, limit: 5, offset: 10 };
    const fetchFn = fakeFetch(jsonResponse(page));
    const client = createApiClient({ ...CONFIG, fetchFn });
    const result = await client.listSessions({
      playerId: "p1",
      dateFrom: "2026-07-01",
      dateTo: "2026-07-09",
      limit: 5,
      offset: 10,
    });
    expect(result).toEqual(page);
    const [url, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0] as [
      string,
      RequestInit,
    ];
    expect(url).toBe(
      "http://lab:8000/sessions?player_id=p1&date_from=2026-07-01&date_to=2026-07-09&limit=5&offset=10",
    );
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer tok");
  });

  it("omits undefined query params and strips a trailing base slash", async () => {
    const fetchFn = fakeFetch(jsonResponse({ items: [], total: 0, limit: 50, offset: 0 }));
    const client = createApiClient({ baseUrl: "http://lab:8000/", token: "tok", fetchFn });
    await client.listSessions();
    const [url] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0] as [string];
    expect(url).toBe("http://lab:8000/sessions");
  });

  it("fetches session, tags, events, clips, videos and ball metrics by path", async () => {
    const calls: string[] = [];
    const fetchFn = vi.fn(async (url: string) => {
      calls.push(url);
      return jsonResponse([]);
    }) as unknown as typeof fetch;
    const client = createApiClient({ ...CONFIG, fetchFn });
    await client.getSession("s1");
    await client.listTags("s1");
    await client.listEvents("s1");
    await client.listClips("s1");
    await client.listSessionVideos("s1");
    await client.ballMetrics("s1", 7);
    expect(calls).toEqual([
      "http://lab:8000/sessions/s1",
      "http://lab:8000/sessions/s1/tags",
      "http://lab:8000/sessions/s1/events",
      "http://lab:8000/sessions/s1/clips",
      "http://lab:8000/sessions/s1/videos",
      "http://lab:8000/sessions/s1/balls/7/metrics",
    ]);
  });

  it("uses the global fetch when no fetchFn is injected", async () => {
    const spy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ items: [], total: 0, limit: 50, offset: 0 }));
    const client = createApiClient(CONFIG);
    await client.listSessions();
    expect(spy).toHaveBeenCalledOnce();
    spy.mockRestore();
  });

  it("throws ApiError with the server detail on non-2xx", async () => {
    const fetchFn = fakeFetch(jsonResponse({ detail: "session not found" }, 404));
    const client = createApiClient({ ...CONFIG, fetchFn });
    const error = await client.getSession("nope").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(404);
    expect((error as ApiError).message).toBe("API 404: session not found");
  });

  it("falls back to statusText when the error body has no string detail", async () => {
    const fetchFn = fakeFetch(
      new Response(JSON.stringify({ detail: 42 }), { status: 500, statusText: "Server Error" }),
    );
    const client = createApiClient({ ...CONFIG, fetchFn });
    const error = (await client.getSession("s1").catch((e: unknown) => e)) as ApiError;
    expect(error.message).toBe("API 500: Server Error");
  });

  it("falls back to statusText when the error body is not JSON", async () => {
    const fetchFn = fakeFetch(new Response("boom", { status: 502, statusText: "Bad Gateway" }));
    const client = createApiClient({ ...CONFIG, fetchFn });
    const error = (await client.getSession("s1").catch((e: unknown) => e)) as ApiError;
    expect(error.message).toBe("API 502: Bad Gateway");
  });

  it.each([
    ["JSON null", "null"],
    ["JSON string scalar", '"oops"'],
    ["JSON object without detail", '{"error":"x"}'],
  ])("falls back to statusText when the error body is %s", async (_label, body) => {
    const fetchFn = fakeFetch(
      new Response(body, { status: 503, statusText: "Service Unavailable" }),
    );
    const client = createApiClient({ ...CONFIG, fetchFn });
    const error = (await client.getSession("s1").catch((e: unknown) => e)) as ApiError;
    expect(error.message).toBe("API 503: Service Unavailable");
  });
});

describe("environment defaults", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("targets the same-origin proxy with no token (US-L3: the browser never holds it)", () => {
    expect(API_PROXY_BASE).toBe("/api/cricai");
    expect(apiBase()).toBe("/api/cricai");
    expect(apiToken()).toBe("");
    expect(authHeaders()).toEqual({});
    expect(defaultConfig()).toEqual({ baseUrl: "/api/cricai", token: "" });
  });

  it("ignores the retired NEXT_PUBLIC_API_* variables", () => {
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "http://lab:9999");
    vi.stubEnv("NEXT_PUBLIC_API_TOKEN", "leaked");
    expect(defaultConfig()).toEqual({ baseUrl: "/api/cricai", token: "" });
    expect(authHeaders()).toEqual({});
  });

  it("defaultMediaBase reads the env var with a LAN fallback", () => {
    vi.stubEnv("NEXT_PUBLIC_CRICAI_MEDIA_BASE", "http://nas:9000/clips");
    expect(defaultMediaBase()).toBe("http://nas:9000/clips");
    vi.stubEnv("NEXT_PUBLIC_CRICAI_MEDIA_BASE", undefined);
    expect(defaultMediaBase()).toBe("http://localhost:9000/cricai");
  });
});

describe("clipMediaUrl", () => {
  it("joins base and key with exactly one slash", () => {
    expect(clipMediaUrl("http://nas:9000/cricai", "clips/s1/b1_C1.mp4")).toBe(
      "http://nas:9000/cricai/clips/s1/b1_C1.mp4",
    );
    expect(clipMediaUrl("http://nas:9000/cricai/", "/clips/s1/b1_C1.mp4")).toBe(
      "http://nas:9000/cricai/clips/s1/b1_C1.mp4",
    );
  });
});

// US-J5 review queue + US-G3/H5 publish gate additions.

const QUEUE_ITEM: ReviewQueueItemOut = {
  id: "r1",
  player_id: "p1",
  session_id: "s1",
  kind: "daily",
  period_start: "2026-07-09",
  period_end: "2026-07-09",
  review_due_at: "2026-07-10T18:00:00Z",
  created_at: "2026-07-09T19:00:00Z",
};

describe("listReviewQueue", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("fetches the coach review queue with the auth header (US-J5)", async () => {
    const fetchFn = fakeFetch(jsonResponse([QUEUE_ITEM]));
    await expect(listReviewQueue({ ...CONFIG, fetchFn })).resolves.toEqual([QUEUE_ITEM]);
    const [url, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0] as [
      string,
      RequestInit,
    ];
    expect(url).toBe("http://lab:8000/settings/review-queue");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer tok");
  });

  it("defaults to the same-origin proxy without a bearer", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse([]));
    await expect(listReviewQueue()).resolves.toEqual([]);
    const [url, init] = spy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/cricai/settings/review-queue");
    expect(init.headers).not.toHaveProperty("Authorization");
    spy.mockRestore();
  });

  it("propagates a role refusal as ApiError (server-enforced coach gate)", async () => {
    const fetchFn = fakeFetch(jsonResponse({ detail: "requires one of: [coach]" }, 403));
    const error = (await listReviewQueue({ ...CONFIG, fetchFn }).catch(
      (e: unknown) => e,
    )) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(403);
  });
});

describe("publishReport", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("POSTs the publish gate and resolves the published outcome", async () => {
    const fetchFn = fakeFetch(jsonResponse({ status: "published", reasons: [] }));
    await expect(publishReport("r1", { ...CONFIG, fetchFn })).resolves.toEqual({
      status: "published",
      reasons: [],
    });
    const [url, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0] as [
      string,
      RequestInit,
    ];
    expect(url).toBe("http://lab:8000/reports/r1/publish");
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer tok");
  });

  it("resolves the 409 BLOCKED outcome with its reasons — a decision, not an error", async () => {
    const blocked = { status: "blocked", reasons: ["claims check failed: control_pct"] };
    const fetchFn = fakeFetch(jsonResponse(blocked, 409));
    await expect(publishReport("r1", { ...CONFIG, fetchFn })).resolves.toEqual(blocked);
  });

  it("throws ApiError on any other refusal", async () => {
    const fetchFn = fakeFetch(jsonResponse({ detail: "report not found" }, 404));
    const error = (await publishReport("nope", { ...CONFIG, fetchFn }).catch(
      (e: unknown) => e,
    )) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.message).toBe("API 404: report not found");
  });

  // A 409 (or 2xx) whose body is not a PublishOut decision — an intermediary
  // proxy's conflict page, a future second 409 source — must throw so the
  // queue renders an honest failure instead of a silent dead button.
  it.each([
    ["a JSON string", "conflict"],
    ["a JSON null", null],
    ["a detail-only object", { detail: "upstream conflict" }],
    ["an unknown status", { status: "weird", reasons: [] }],
    ["missing reasons", { status: "blocked" }],
    ["non-string reasons", { status: "blocked", reasons: [7] }],
  ])("throws ApiError when the 409 body is %s, not a decision", async (_label, body) => {
    const fetchFn = fakeFetch(jsonResponse(body, 409));
    const error = (await publishReport("r1", { ...CONFIG, fetchFn }).catch(
      (e: unknown) => e,
    )) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(409);
    expect(error.message).toContain("HTTP 409");
  });

  it("throws ApiError when the decision body is not JSON at all", async () => {
    const fetchFn = fakeFetch(new Response("Conflict", { status: 409, statusText: "Conflict" }));
    const error = (await publishReport("r1", { ...CONFIG, fetchFn }).catch(
      (e: unknown) => e,
    )) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(409);
    expect(error.message).toContain("HTTP 409");
  });

  it("strips a trailing base slash and defaults to the environment config", async () => {
    const fetchFn = fakeFetch(jsonResponse({ status: "published", reasons: [] }));
    await publishReport("r1", { baseUrl: "http://lab:8000/", token: "tok", fetchFn });
    expect((fetchFn as ReturnType<typeof vi.fn>).mock.calls[0][0]).toBe(
      "http://lab:8000/reports/r1/publish",
    );
    const spy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ status: "published", reasons: [] }));
    await publishReport("r2");
    const [url, init] = spy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/cricai/reports/r2/publish");
    expect(init.headers).toEqual({});
    spy.mockRestore();
  });
});

describe("apiRequest", () => {
  it("GETs JSON through the proxy by default, dropping undefined query params", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse([{ id: "a" }]));
    await expect(
      apiRequest("/alerts", { query: { audience: "parent", since: undefined } }),
    ).resolves.toEqual([{ id: "a" }]);
    expect(spy).toHaveBeenCalledWith("/api/cricai/alerts?audience=parent", {
      method: "GET",
      headers: { Accept: "application/json" },
      body: undefined,
    });
    spy.mockRestore();
  });

  it("sends a JSON body with its content type and the config bearer", async () => {
    const fetchFn = fakeFetch(jsonResponse({ id: "w1" }, 201));
    await expect(
      apiRequest("/wellness", { method: "POST", body: { soreness: 2 } }, { ...CONFIG, fetchFn }),
    ).resolves.toEqual({ id: "w1" });
    expect(fetchFn).toHaveBeenCalledWith("http://lab:8000/wellness", {
      method: "POST",
      headers: {
        Accept: "application/json",
        Authorization: "Bearer tok",
        "Content-Type": "application/json",
      },
      body: '{"soreness":2}',
    });
  });

  it("resolves undefined on 204", async () => {
    const fetchFn = fakeFetch(new Response(null, { status: 204 }));
    await expect(
      apiRequest("/notes/n1", { method: "DELETE" }, { ...CONFIG, fetchFn }),
    ).resolves.toBeUndefined();
  });

  it("throws ApiError with the server detail", async () => {
    const fetchFn = fakeFetch(jsonResponse({ detail: "requires one of: [parent]" }, 403));
    const error = (await apiRequest("/cameras", {}, { ...CONFIG, fetchFn }).catch(
      (e: unknown) => e,
    )) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(403);
    expect(error.message).toBe("API 403: requires one of: [parent]");
  });
});
