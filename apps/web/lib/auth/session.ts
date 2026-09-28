/**
 * The cricai_session cookie: the role and its API token, set by /login and
 * read only on the server (proxy route handler, middleware, root layout). It
 * is httpOnly, so no browser script can read the token (US-L3). The API does
 * not report a role, so the cookie carries it alongside the token.
 *
 * Edge-safe (middleware runs there): no Node Buffer, only btoa/atob.
 */
import { isRole, type Role } from "./roles";

export const SESSION_COOKIE = "cricai_session";

/** Sign-in lasts 30 days on a device; the API still checks the token on every call. */
export const SESSION_MAX_AGE_S = 60 * 60 * 24 * 30;

export interface SessionData {
  role: Role;
  token: string;
}

function toBase64Url(text: string): string {
  const bytes = new TextEncoder().encode(text);
  const binary = Array.from(bytes, (byte) => String.fromCharCode(byte)).join("");
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromBase64Url(value: string): string {
  const binary = atob(value.replace(/-/g, "+").replace(/_/g, "/"));
  return new TextDecoder().decode(Uint8Array.from(binary, (char) => char.charCodeAt(0)));
}

export function encodeSession(session: SessionData): string {
  return toBase64Url(JSON.stringify({ role: session.role, token: session.token }));
}

/** The session in a cookie value, or null for a missing, malformed or tampered one. */
export function decodeSession(value: string | undefined): SessionData | null {
  if (!value) {
    return null;
  }
  try {
    const parsed: unknown = JSON.parse(fromBase64Url(value));
    if (typeof parsed !== "object" || parsed === null) {
      return null;
    }
    const { role, token } = parsed as { role?: unknown; token?: unknown };
    return isRole(role) && typeof token === "string" && token !== "" ? { role, token } : null;
  } catch {
    return null;
  }
}

export interface SessionCookieOptions {
  httpOnly: true;
  sameSite: "strict";
  secure: boolean;
  path: "/";
  maxAge: number;
}

/**
 * Cookie attributes. `secure` follows the request scheme: the lab LAN is
 * plain http, where a Secure cookie would never be stored.
 */
export function sessionCookieOptions(secure: boolean, maxAge = SESSION_MAX_AGE_S): SessionCookieOptions {
  return { httpOnly: true, sameSite: "strict", secure, path: "/", maxAge };
}
