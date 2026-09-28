import { render, screen, within } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import RootLayout, { metadata, viewport } from "./layout";

vi.mock("next/navigation", () => ({ usePathname: () => "/" }));

type BodyElement = ReactElement<{ children: ReactNode }>;
type HtmlElement = ReactElement<{ lang: string; children: BodyElement }>;

describe("RootLayout (app shell, US-K1)", () => {
  it("wraps children in an English html/body with the app shell", () => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false }) as MediaQueryList));
    const markup = RootLayout({ children: <p>page content</p> }) as HtmlElement;
    expect(markup.props.lang).toBe("en");
    render(<>{markup.props.children.props.children}</>);
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
    vi.unstubAllGlobals();
  });

  it("declares the lab metadata and a device-width viewport", () => {
    expect(metadata.title).toBe("cricAI — Home Cricket Lab");
    expect(viewport.width).toBe("device-width");
  });
});
