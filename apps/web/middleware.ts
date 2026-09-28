/**
 * Pages need a sign-in: without a valid cricai_session cookie every page
 * redirects to /login, remembering where the user was going. The API proxy
 * (/api/*) answers 401 itself, and static assets pass through.
 */
import { NextResponse, type NextRequest } from "next/server";
import { decodeSession, SESSION_COOKIE } from "@/lib/auth/session";

export function middleware(request: NextRequest): NextResponse {
  if (decodeSession(request.cookies.get(SESSION_COOKIE)?.value) !== null) {
    return NextResponse.next();
  }
  const destination = request.nextUrl.pathname + request.nextUrl.search;
  const login = request.nextUrl.clone();
  login.pathname = "/login";
  login.search = "";
  if (destination !== "/") {
    login.searchParams.set("next", destination);
  }
  return NextResponse.redirect(login);
}

export const config = {
  matcher: [
    "/((?!api/|_next/|login|favicon\\.ico|icons/|manifest\\.webmanifest|robots\\.txt).*)",
  ],
};
