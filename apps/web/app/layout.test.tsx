import { render, screen } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { describe, expect, it } from "vitest";
import RootLayout, { metadata } from "./layout";

type BodyElement = ReactElement<{ children: ReactNode }>;
type HtmlElement = ReactElement<{ lang: string; children: BodyElement }>;

describe("RootLayout (nav shell, US-K1)", () => {
  it("wraps children in an English html/body with the primary nav", () => {
    const markup = RootLayout({ children: <p>page content</p> }) as HtmlElement;
    expect(markup.props.lang).toBe("en");
    render(<>{markup.props.children.props.children}</>);
    expect(screen.getByRole("navigation", { name: "primary" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "cricAI" })).toHaveAttribute("href", "/");
    expect(screen.getByRole("link", { name: "Sessions" })).toHaveAttribute("href", "/sessions");
    // US-J5: the coach review queue must be reachable from the primary nav —
    // without this link the page only exists for whoever knows the URL.
    expect(screen.getByRole("link", { name: "Review queue" })).toHaveAttribute("href", "/review");
    expect(screen.getByText("page content")).toBeInTheDocument();
  });

  it("declares the lab metadata", () => {
    expect(metadata.title).toBe("cricAI — Home Cricket Lab");
  });
});
