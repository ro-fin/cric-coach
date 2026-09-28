import { describe, expect, it } from "vitest";
import { safeNextPath } from "./next";
import {
  decodeSession,
  encodeSession,
  SESSION_COOKIE,
  SESSION_MAX_AGE_S,
  sessionCookieOptions,
} from "./session";

describe("session cookie codec", () => {
  it("round-trips role and token, including non-ASCII tokens", () => {
    for (const session of [
      { role: "coach" as const, token: "c0ach-t0ken+/=" },
      { role: "player" as const, token: "tök€n-ünïcode" },
    ]) {
      const value = encodeSession(session);
      expect(value).toMatch(/^[A-Za-z0-9_-]+$/);
      expect(decodeSession(value)).toEqual(session);
    }
  });

  it("rejects missing, malformed and tampered values", () => {
    const b64 = (text: string) => btoa(text).replace(/=+$/, "");
    expect(decodeSession(undefined)).toBeNull();
    expect(decodeSession("")).toBeNull();
    expect(decodeSession("%%%not-base64")).toBeNull();
    expect(decodeSession(b64("not json"))).toBeNull();
    expect(decodeSession(b64("null"))).toBeNull();
    expect(decodeSession(b64("7"))).toBeNull();
    expect(decodeSession(b64(JSON.stringify({ role: "admin", token: "t" })))).toBeNull();
    expect(decodeSession(b64(JSON.stringify({ role: "coach", token: "" })))).toBeNull();
    expect(decodeSession(b64(JSON.stringify({ role: "coach", token: 5 })))).toBeNull();
  });

  it("uses httpOnly, SameSite=Strict, path / and a 30-day lifetime", () => {
    expect(SESSION_COOKIE).toBe("cricai_session");
    expect(sessionCookieOptions(false)).toEqual({
      httpOnly: true,
      sameSite: "strict",
      secure: false,
      path: "/",
      maxAge: SESSION_MAX_AGE_S,
    });
    expect(sessionCookieOptions(true, 0)).toMatchObject({ secure: true, maxAge: 0 });
    expect(SESSION_MAX_AGE_S).toBe(2592000);
  });
});

describe("safeNextPath", () => {
  it.each([
    [undefined, "/"],
    ["", "/"],
    ["https://evil.example/", "/"],
    ["//evil.example", "/"],
    ["/\\evil.example", "/"],
    ["/login?next=/x", "/"],
    ["sessions", "/"],
    ["/sessions/abc?ball=3", "/sessions/abc?ball=3"],
    [["/reports", "/other"], "/reports"],
  ])("maps %j to %s", (input, expected) => {
    expect(safeNextPath(input as string | string[] | undefined)).toBe(expected);
  });
});
