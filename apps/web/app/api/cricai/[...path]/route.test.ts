// @vitest-environment node
import { describe, expect, it, vi } from "vitest";
import { encodeSession } from "@/lib/auth/session";
import { DELETE, dynamic, GET, HEAD, PATCH, POST, PUT } from "./route";

describe("/api/cricai/[...path] route", () => {
  it("hands every method to the proxy with the catch-all path", async () => {
    const calls: Array<[string, string | undefined]> = [];
    vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
      calls.push([url, init?.method]);
      return new Response("{}", { status: 200 });
    });
    vi.stubEnv("CRICAI_API_BASE_URL", "http://api.lab:8000");
    const cookie = `cricai_session=${encodeSession({ role: "parent", token: "p" })}`;
    const handlers = { GET, HEAD, POST, PUT, PATCH, DELETE };
    for (const [method, handler] of Object.entries(handlers)) {
      const response = await handler(
        new Request("http://dash.lab/api/cricai/sessions/s1", { method, headers: { cookie } }),
        { params: Promise.resolve({ path: ["sessions", "s1"] }) },
      );
      expect(response.status).toBe(200);
    }
    expect(calls).toEqual(
      Object.keys(handlers).map((method) => ["http://api.lab:8000/sessions/s1", method]),
    );
    expect(dynamic).toBe("force-dynamic");
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });
});
