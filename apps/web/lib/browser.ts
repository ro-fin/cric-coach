/**
 * Full-page navigation. Used after sign-in and sign-out, where the root
 * layout must re-read the session cookie on the server, so a client-side
 * router push is not enough. Its own module so tests can replace it.
 */
export function hardNavigate(path: string): void {
  window.location.assign(path);
}
