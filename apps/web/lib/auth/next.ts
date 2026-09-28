/**
 * Where to go after signing in. Only same-origin paths are honoured, so a
 * crafted /login?next=https://evil.example link cannot bounce a user away.
 */
export function safeNextPath(next: string | string[] | undefined): string {
  const value = Array.isArray(next) ? next[0] : next;
  if (
    typeof value !== "string" ||
    !value.startsWith("/") ||
    value.startsWith("//") ||
    value.startsWith("/\\") ||
    value.startsWith("/login")
  ) {
    return "/";
  }
  return value;
}
