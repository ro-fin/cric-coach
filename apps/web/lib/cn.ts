/** Join class names, dropping falsy entries (conditional classes read inline). */
export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}
