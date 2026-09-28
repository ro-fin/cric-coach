// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";
import { encodeSession } from "@/lib/auth/session";
import {
  DEFAULT_API_BASE_URL,
  FORWARDED_REQUEST_HEADERS,
  proxy,
  readSessionCookie,
  requestOrigin,
} from "./proxy";

const API = "http://api.lab:8000";
const COACH = encodeSession({ role: "coach", token: "coach-secret" });

function req(
  path: string,
  init: { method?: string; cookie?: string; headers?: Record<string, string>; body?: string } = {},
): Request {
  const headers = new Headers(init.headers);
  if (init.cookie !== undefined) {
    headers.set("cookie", init.cookie);
  }
  return new Request(`http://dash.lab:3000/api/cricai/${path}`, {
    method: init.method ?? "GET",
    headers,
    body: init.body,
  });
}

function upstream(status: number, body: string | null = "{}", headers: Record<string, string> = {}) {
  return vi.fn(async () => new Response(body, { status, headers }));
}

function sentTo(fetchFn: ReturnType<typeof vi.fn>, call = 0): { url: string; init: RequestInit & { headers: Headers } } {
  const [url, init] = fetchFn.mock.calls[call] as [string, RequestInit & { headers: Headers }];
  return { url, init };
}

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("proxy forwarding", () => {
  it("forwards to the API with the bearer from the cookie only", async () => {
    const fetchFn = upstream(200, '{"items":[]}', { "content-type": "application/json" });
    const response = await proxy(
      req("sessions?limit=5&offset=0", {
        cookie: `theme=dark; cricai_session=${COACH}; other=1`,
        headers: { authorization: "Bearer browser-forged", accept: "application/json" },
      }),
      ["sessions"],
      { fetchFn, apiBaseUrl: `${API}/` },
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ items: [] });
    const { url, init } = sentTo(fetchFn);
    expect(url).toBe(`${API}/sessions?limit=5&offset=0`);
    expect(init.method).toBe("GET");
    expect(init.body).toBeUndefined();
    // The browser's Authorization header is stripped; the cookie's token is used.
    expect(init.headers.get("authorization")).toBe("Bearer coach-secret");
    expect(init.headers.get("accept")).toBe("application/json");
  });

  it("forwards only allow-listed request headers", async () => {
    const fetchFn = upstream(200);
    const allowed = Object.fromEntries(FORWARDED_REQUEST_HEADERS.map((name) => [name, `v-${name}`]));
    await proxy(
      req("sessions", {
        cookie: `cricai_session=${COACH}`,
        headers: { ...allowed, "x-forwarded-for": "10.0.0.9", "x-custom": "1", origin: "http://dash.lab:3000" },
      }),
      ["sessions"],
      { fetchFn, apiBaseUrl: API },
    );
    const sent = sentTo(fetchFn).init.headers;
    const names = [...sent.keys()].sort();
    expect(names).toEqual([...FORWARDED_REQUEST_HEADERS, "authorization"].sort());
    expect(sent.get("cookie")).toBeNull();
    expect(sent.get("range")).toBe("v-range");
  });

  it("forwards only allow-listed response headers", async () => {
    const fetchFn = upstream(200, "%PDF", {
      "content-type": "application/pdf",
      "content-disposition": 'attachment; filename="r.pdf"',
      "set-cookie": "api=1",
      server: "uvicorn",
      "x-internal": "1",
    });
    const response = await proxy(req("reports/r1/export?format=pdf", { cookie: `cricai_session=${COACH}` }), ["reports", "r1", "export"], {
      fetchFn,
      apiBaseUrl: API,
    });
    expect(response.headers.get("content-type")).toBe("application/pdf");
    expect(response.headers.get("content-disposition")).toBe('attachment; filename="r.pdf"');
    expect(response.headers.get("set-cookie")).toBeNull();
    expect(response.headers.get("server")).toBeNull();
    expect(response.headers.get("x-internal")).toBeNull();
    expect(await response.text()).toBe("%PDF");
  });

  it("passes a JSON body and method through", async () => {
    const fetchFn = upstream(201, '{"id":"n1"}');
    const response = await proxy(
      req("notes", {
        method: "POST",
        cookie: `cricai_session=${COACH}`,
        headers: { "content-type": "application/json", origin: "http://dash.lab:3000" },
        body: '{"body":"hi"}',
      }),
      ["notes"],
      { fetchFn, apiBaseUrl: API },
    );
    expect(response.status).toBe(201);
    const { init } = sentTo(fetchFn);
    expect(init.method).toBe("POST");
    expect(new TextDecoder().decode(init.body as ArrayBuffer)).toBe('{"body":"hi"}');
    expect(init.headers.get("content-type")).toBe("application/json");
  });

  it("returns empty bodies for 204, 304 and HEAD", async () => {
    for (const [status, method] of [
      [204, "DELETE"],
      [304, "GET"],
      [200, "HEAD"],
    ] as const) {
      const fetchFn = vi.fn(async () => new Response(status === 200 ? null : null, { status }));
      const response = await proxy(req("notes/n1", { method, cookie: `cricai_session=${COACH}` }), ["notes", "n1"], {
        fetchFn,
        apiBaseUrl: API,
      });
      expect(response.status).toBe(status);
      expect(await response.text()).toBe("");
    }
  });

  it("encodes path segments", async () => {
    const fetchFn = upstream(200);
    await proxy(req("notes/a%20b", { cookie: `cricai_session=${COACH}` }), ["notes", "a b?x"], { fetchFn, apiBaseUrl: API });
    expect(sentTo(fetchFn).url).toBe(`${API}/notes/a%20b%3Fx`);
  });

  it("reads the API base from CRICAI_API_BASE_URL, with a localhost default", async () => {
    const fetchFn = upstream(200);
    vi.stubEnv("CRICAI_API_BASE_URL", "http://env.lab:9000");
    await proxy(req("sessions", { cookie: `cricai_session=${COACH}` }), ["sessions"], { fetchFn });
    expect(sentTo(fetchFn).url).toBe("http://env.lab:9000/sessions");
    vi.stubEnv("CRICAI_API_BASE_URL", undefined);
    await proxy(req("sessions", { cookie: `cricai_session=${COACH}` }), ["sessions"], { fetchFn });
    expect(sentTo(fetchFn, 1).url).toBe(`${DEFAULT_API_BASE_URL}/sessions`);
  });

  it("uses the global fetch by default", async () => {
    const fetchFn = upstream(200);
    vi.stubGlobal("fetch", fetchFn);
    await proxy(req("sessions", { cookie: `cricai_session=${COACH}` }), ["sessions"], { apiBaseUrl: API });
    expect(fetchFn).toHaveBeenCalledOnce();
    // Sign-in probes too (both answer 200 here, which reads as parent).
    const signIn = await proxy(
      req("_session", { method: "POST", body: '{"role":"parent","token":"p"}' }),
      ["_session"],
      { apiBaseUrl: API },
    );
    expect(signIn.status).toBe(200);
    expect(fetchFn).toHaveBeenCalledTimes(3);
    vi.unstubAllGlobals();
  });
});

describe("proxy auth", () => {
  it("answers 401 without calling the API when there is no session", async () => {
    const fetchFn = upstream(200);
    const response = await proxy(req("sessions", { headers: { authorization: "Bearer forged" } }), ["sessions"], {
      fetchFn,
      apiBaseUrl: API,
    });
    expect(response.status).toBe(401);
    expect(await response.json()).toEqual({ detail: "Not signed in" });
    expect(response.headers.get("set-cookie")).toBeNull();
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it("clears a malformed session cookie", async () => {
    const fetchFn = upstream(200);
    const response = await proxy(req("sessions", { cookie: "cricai_session=garbage" }), ["sessions"], { fetchFn, apiBaseUrl: API });
    expect(response.status).toBe(401);
    expect(response.headers.get("set-cookie")).toMatch(/^cricai_session=; Path=\/; Max-Age=0; HttpOnly; SameSite=Strict$/);
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it("clears the cookie when the API rejects the token", async () => {
    const fetchFn = upstream(401, '{"detail":"invalid token"}', { "content-type": "application/json" });
    const response = await proxy(req("sessions", { cookie: `cricai_session=${COACH}` }), ["sessions"], { fetchFn, apiBaseUrl: API });
    expect(response.status).toBe(401);
    expect(await response.json()).toEqual({ detail: "invalid token" });
    expect(response.headers.get("set-cookie")).toContain("cricai_session=; Path=/; Max-Age=0");
  });

  it("keeps the cookie on a 403 (role refusal is not a sign-out)", async () => {
    const fetchFn = upstream(403, '{"detail":"requires one of: [coach]"}');
    const response = await proxy(req("settings/review-queue", { cookie: `cricai_session=${COACH}` }), ["settings", "review-queue"], {
      fetchFn,
      apiBaseUrl: API,
    });
    expect(response.status).toBe(403);
    expect(response.headers.get("set-cookie")).toBeNull();
  });

  it("marks cookies Secure on https", async () => {
    const response = await proxy(
      new Request("https://dash.lab/api/cricai/sessions", { headers: { cookie: "cricai_session=bad" } }),
      ["sessions"],
      { fetchFn: upstream(200), apiBaseUrl: API },
    );
    expect(response.headers.get("set-cookie")).toMatch(/; Secure$/);
  });

  it("accepts a same-origin sign-in when next start's request.url says localhost (regression)", async () => {
    // Under `next start`, request.url carries the bind address, not the host the
    // tablet used; the Origin must be compared with the Host header instead.
    const response = await proxy(
      new Request("http://localhost:3000/api/cricai/_session", {
        method: "POST",
        headers: {
          host: "lab.local:3000",
          origin: "http://lab.local:3000",
          "content-type": "application/json",
        },
        body: '{"role":"parent","token":"p"}',
      }),
      ["_session"],
      { fetchFn: upstream(200), apiBaseUrl: API },
    );
    expect(response.status).toBe(200);
    expect(response.headers.get("set-cookie")).not.toMatch(/Secure/);
  });

  it("honours X-Forwarded-Host/Proto behind a TLS reverse proxy", async () => {
    const response = await proxy(
      new Request("http://localhost:3000/api/cricai/_session", {
        method: "POST",
        headers: {
          host: "localhost:3000",
          "x-forwarded-host": "cricai.lab, inner",
          "x-forwarded-proto": "https",
          origin: "https://cricai.lab",
          "content-type": "application/json",
        },
        body: '{"role":"parent","token":"p"}',
      }),
      ["_session"],
      { fetchFn: upstream(200), apiBaseUrl: API },
    );
    expect(response.status).toBe(200);
    expect(response.headers.get("set-cookie")).toMatch(/; Secure$/);
  });

  it("falls back to request.url when no Host header is present", () => {
    const request = new Request("http://dash.lab:3000/api/cricai/x");
    request.headers.delete("host");
    expect(requestOrigin(request)).toBe("http://dash.lab:3000");
  });

  it("refuses cross-origin state changes", async () => {
    const fetchFn = upstream(200);
    const response = await proxy(
      req("notes", { method: "POST", cookie: `cricai_session=${COACH}`, headers: { origin: "http://evil.example" }, body: "{}" }),
      ["notes"],
      { fetchFn, apiBaseUrl: API },
    );
    expect(response.status).toBe(403);
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it.each([[[]], [["sessions", ".."]], [["sessions", "."]], [["sessions", ""]], [["_internal"]]])(
    "refuses unsafe or reserved paths %j",
    async (path) => {
      const fetchFn = upstream(200);
      const response = await proxy(req("x", { cookie: `cricai_session=${COACH}` }), path, { fetchFn, apiBaseUrl: API });
      expect(response.status).toBe(404);
      expect(fetchFn).not.toHaveBeenCalled();
    },
  );

  it("answers 502 when the API is unreachable", async () => {
    const fetchFn = vi.fn(async () => {
      throw new TypeError("fetch failed");
    });
    const response = await proxy(req("sessions", { cookie: `cricai_session=${COACH}` }), ["sessions"], { fetchFn, apiBaseUrl: API });
    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ detail: "The cricAI API could not be reached." });
  });
});

describe("readSessionCookie", () => {
  it("finds the session cookie among others, keeping '=' in the value", () => {
    expect(readSessionCookie(req("x", { cookie: "a=1; cricai_session=ab=c; b=2" }))).toBe("ab=c");
    expect(readSessionCookie(req("x", { cookie: "a=1" }))).toBeUndefined();
    expect(readSessionCookie(req("x"))).toBeUndefined();
  });
});

describe("_session endpoint", () => {
  /** Fake API: token -> status per probe path. */
  function api(statuses: Record<string, [number, number]>) {
    return vi.fn(async (url: string, init: RequestInit) => {
      const token = new Headers(init.headers).get("authorization")?.replace("Bearer ", "") ?? "";
      const [audit, queue] = statuses[token] ?? [401, 401];
      return new Response("[]", { status: url.endsWith("/privacy/audit") ? audit : queue });
    });
  }
  const TOKENS = { "p-tok": [200, 403], "c-tok": [403, 200], "k-tok": [403, 403] } as Record<string, [number, number]>;

  function signIn(body: unknown, fetchFn = api(TOKENS), url = "http://dash.lab:3000/api/cricai/_session") {
    return proxy(
      new Request(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: typeof body === "string" ? body : JSON.stringify(body),
      }),
      ["_session"],
      { fetchFn: fetchFn as unknown as typeof fetch, apiBaseUrl: API },
    );
  }

  it.each([
    ["parent", "p-tok"],
    ["coach", "c-tok"],
    ["player", "k-tok"],
  ])("signs a %s in and sets the httpOnly cookie", async (role, token) => {
    const fetchFn = api(TOKENS);
    const response = await signIn({ role, token: ` ${token} ` }, fetchFn);
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ role });
    const cookie = response.headers.get("set-cookie") ?? "";
    expect(cookie).toMatch(/^cricai_session=[A-Za-z0-9_-]+; Path=\/; Max-Age=2592000; HttpOnly; SameSite=Strict$/);
    const value = cookie.split(";")[0].split("=")[1];
    const session = await proxy(req("_session", { cookie: `cricai_session=${value}` }), ["_session"]);
    expect(await session.json()).toEqual({ role });
    // Both probes carried the trimmed token.
    expect(fetchFn.mock.calls.map(([url]) => url).sort()).toEqual([`${API}/privacy/audit`, `${API}/settings/review-queue`]);
  });

  it("refuses a token of another role", async () => {
    const response = await signIn({ role: "coach", token: "k-tok" });
    expect(response.status).toBe(403);
    expect(await response.json()).toEqual({ detail: "That token is not a coach token." });
    expect(response.headers.get("set-cookie")).toBeNull();
  });

  it("refuses a token the API does not accept", async () => {
    const response = await signIn({ role: "player", token: "nope" });
    expect(response.status).toBe(401);
    expect(await response.json()).toEqual({ detail: "That token was not accepted." });
  });

  it.each([[{ role: "admin", token: "x" }], [{ role: "coach", token: "  " }], [{ role: "coach" }], ["not json"], [null]])(
    "rejects a bad sign-in body %j",
    async (body) => {
      const fetchFn = api(TOKENS);
      const response = await signIn(body, fetchFn);
      expect(response.status).toBe(400);
      expect(fetchFn).not.toHaveBeenCalled();
    },
  );

  it("reports an unreachable or misbehaving API as 502", async () => {
    const down = vi.fn(async () => {
      throw new TypeError("fetch failed");
    });
    expect((await signIn({ role: "coach", token: "c-tok" }, down)).status).toBe(502);
    const broken = api({ "c-tok": [500, 500] });
    expect((await signIn({ role: "coach", token: "c-tok" }, broken)).status).toBe(502);
    // One probe refused, the other failing: not provably a player.
    const half = api({ "k-tok": [403, 503] });
    expect((await signIn({ role: "player", token: "k-tok" }, half)).status).toBe(502);
  });

  it("sets a Secure cookie over https", async () => {
    const response = await signIn({ role: "coach", token: "c-tok" }, api(TOKENS), "https://dash.lab/api/cricai/_session");
    expect(response.headers.get("set-cookie")).toMatch(/; Secure$/);
  });

  it("returns a null role when signed out", async () => {
    const response = await proxy(req("_session"), ["_session"]);
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ role: null });
    expect(response.headers.get("cache-control")).toBe("no-store");
  });

  it("signs out by clearing the cookie", async () => {
    const response = await proxy(req("_session", { method: "DELETE", cookie: `cricai_session=${COACH}` }), ["_session"]);
    expect(response.status).toBe(204);
    expect(response.headers.get("set-cookie")).toContain("Max-Age=0");
  });

  it("allows only GET, POST and DELETE", async () => {
    const response = await proxy(req("_session", { method: "PUT", body: "{}" }), ["_session"]);
    expect(response.status).toBe(405);
    expect(response.headers.get("allow")).toBe("GET, POST, DELETE");
  });
});
