import { render, screen, within } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { encodeSession } from "@/lib/auth/session";
import RootLayout, { metadata, viewport } from "./layout";

const jar = vi.hoisted(() => ({ value: undefined as string | undefined }));
vi.mock("next/navigation", () => ({ usePathname: () => "/" }));
vi.mock("next/headers", () => ({
  cookies: async () => ({
    get: (name: string) =>
      name === "cricai_session" && jar.value !== undefined ? { name, value: jar.value } : undefined,
  }),
}));

type ServerElement = ReactElement<{ children: ReactNode }, (props: { children: ReactNode }) => Promise<ReactElement>>;
type BodyElement = ReactElement<{ children: ServerElement }>;
type HtmlElement = ReactElement<{ lang: string; children: BodyElement }>;

/** Render the layout, resolving its async server component the way Next does. */
async function renderLayout() {
  const markup = RootLayout({ children: <p>page content</p> }) as HtmlElement;
  const provider = markup.props.children.props.children;
  render(await provider.type(provider.props));
  return markup;
}

beforeEach(() => {
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false }) as MediaQueryList));
});

afterEach(() => {
  vi.unstubAllGlobals();
  jar.value = undefined;
});

describe("RootLayout (app shell, US-K1)", () => {
  it("wraps children in an English html/body with the app shell", async () => {
    const markup = await renderLayout();
    expect(markup.props.lang).toBe("en");
    const primary = screen.getByRole("navigation", { name: "Primary" });
    expect(screen.getAllByRole("link", { name: "cricAI" })[0]).toHaveAttribute("href", "/");
    expect(within(primary).getByRole("link", { name: "Sessions" })).toHaveAttribute(
      "href",
      "/sessions",
    );
    // US-J5: the coach review queue must be reachable from the primary nav —
    // without this link the page only exists for whoever knows the URL.
    expect(within(primary).getByRole("link", { name: "Review queue" })).toHaveAttribute(
      "href",
      "/review",
    );
    expect(screen.getByText("page content")).toBeInTheDocument();
    // Toasts are available to every page.
    expect(screen.getByRole("status", { name: "Notifications" })).toBeInTheDocument();
    expect(screen.getAllByText("Not signed in").length).toBeGreaterThan(0);
  });

  it("provides the role from the session cookie, never the token", async () => {
    jar.value = encodeSession({ role: "player", token: "k-secret" });
    await renderLayout();
    expect(screen.getAllByText("Signed in as Player").length).toBeGreaterThan(0);
    expect(document.body.innerHTML).not.toContain("k-secret");
    const primary = screen.getByRole("navigation", { name: "Primary" });
    expect(within(primary).queryByRole("link", { name: "Review queue" })).toBeNull();
  });

  it("declares the lab metadata and a device-width viewport", () => {
    expect(metadata.title).toBe("cricAI — Home Cricket Lab");
    expect(viewport.width).toBe("device-width");
  });
});
