// @vitest-environment node
import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";
import { encodeSession } from "@/lib/auth/session";
import { config, middleware } from "./middleware";

function request(path: string, cookie?: string): NextRequest {
  return new NextRequest(`http://dash.lab:3000${path}`, {
    headers: cookie === undefined ? undefined : { cookie },
  });
}

describe("middleware (sign-in required)", () => {
  it("lets a signed-in request through", () => {
    const cookie = `cricai_session=${encodeSession({ role: "player", token: "k" })}`;
    const response = middleware(request("/sessions", cookie));
    expect(response.headers.get("x-middleware-next")).toBe("1");
  });

  it("redirects to /login, remembering the destination", () => {
    const response = middleware(request("/sessions/abc?ball=3"));
    expect(response.status).toBe(307);
    const location = new URL(response.headers.get("location") ?? "");
    expect(location.pathname).toBe("/login");
    expect(location.searchParams.get("next")).toBe("/sessions/abc?ball=3");
  });

  it("redirects home without a next parameter", () => {
    const response = middleware(request("/", "cricai_session=tampered"));
    expect(response.headers.get("location")).toBe("http://dash.lab:3000/login");
  });

  it("does not run for the API, Next assets, the login page or static files", () => {
    const [pattern] = config.matcher;
    const regex = new RegExp(`^${pattern}$`);
    for (const path of ["/", "/sessions", "/reports", "/settings"]) {
      expect(regex.test(path)).toBe(true);
    }
    for (const path of ["/api/cricai/sessions", "/_next/static/x.js", "/login", "/favicon.ico", "/icons/icon-192.png", "/manifest.webmanifest", "/robots.txt"]) {
      expect(regex.test(path)).toBe(false);
    }
  });
});
