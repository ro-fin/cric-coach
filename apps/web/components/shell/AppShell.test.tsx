import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { RoleProvider, type Role } from "@/lib/auth/role";
import { expectAxeClean } from "@/lib/testing/axe";
import { AppShell } from "./AppShell";

const navigation = vi.hoisted(() => ({ pathname: "/" as string | null }));
vi.mock("next/navigation", () => ({ usePathname: () => navigation.pathname }));
// jsdom cannot navigate; an anchor that stays put keeps clicks observable.
vi.mock("next/link", () => ({
  default: ({ href, onClick, ...rest }: ComponentProps<"a">) => (
    <a
      href={href}
      onClick={(event) => {
        event.preventDefault();
        onClick?.(event);
      }}
      {...rest}
    />
  ),
}));

const browser = vi.hoisted(() => ({ hardNavigate: vi.fn() }));
vi.mock("@/lib/browser", () => browser);

function renderShell(role: Role | null, pathname: string | null = "/") {
  navigation.pathname = pathname;
  return render(
    <RoleProvider role={role}>
      <AppShell>
        <main>
          <h1>Page</h1>
        </main>
      </AppShell>
    </RoleProvider>,
  );
}

beforeEach(() => {
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false }) as MediaQueryList));
});

describe("AppShell", () => {
  it("renders the page with a sidebar nav, a tab bar and a skip link", async () => {
    const { container } = renderShell("coach", "/sessions/abc");
    expect(screen.getByRole("heading", { name: "Page" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute("href", "#content");
    const primary = screen.getByRole("navigation", { name: "Primary" });
    const links = within(primary).getAllByRole("link");
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/",
      "/sessions",
      "/progress",
      "/reports",
      "/wellness",
      "/review",
      "/notes",
      "/pipeline",
      "/alerts",
    ]);
    expect(within(primary).getByRole("link", { name: "Sessions" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(primary).getByRole("link", { name: "Today" })).not.toHaveAttribute("aria-current");
    const tabs = screen.getByRole("navigation", { name: "Tabs" });
    expect(within(tabs).getAllByRole("link")).toHaveLength(4);
    await expectAxeClean(container);
  });

  it("shows the signed-in role, or that nobody is signed in", () => {
    renderShell("parent");
    expect(screen.getAllByText("Signed in as Parent").length).toBeGreaterThan(0);
  });

  it("says not signed in and lists every destination without a role", () => {
    renderShell(null);
    expect(screen.getAllByText("Not signed in").length).toBeGreaterThan(0);
    const primary = screen.getByRole("navigation", { name: "Primary" });
    expect(within(primary).getAllByRole("link")).toHaveLength(11);
  });

  it("puts the rest of the destinations under More in a dialog", () => {
    renderShell("parent", "/cameras");
    const tabs = screen.getByRole("navigation", { name: "Tabs" });
    const more = within(tabs).getByRole("button", { name: "More" });
    expect(more.className).toContain("bg-accent");
    fireEvent.click(more);
    const dialog = screen.getByRole("dialog", { name: "More" });
    const cameras = within(dialog).getByRole("link", { name: "Cameras" });
    expect(cameras).toHaveAttribute("aria-current", "page");
    fireEvent.click(cameras);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("marks More inactive when the current page is in the tab bar", () => {
    renderShell("coach", "/");
    const more = within(screen.getByRole("navigation", { name: "Tabs" })).getByRole("button", {
      name: "More",
    });
    expect(more.className).not.toContain("bg-accent");
  });

  it("has no More button when every destination fits (player)", () => {
    renderShell("player", null);
    const tabs = screen.getByRole("navigation", { name: "Tabs" });
    expect(within(tabs).getAllByRole("link")).toHaveLength(5);
    expect(within(tabs).queryByRole("button", { name: "More" })).toBeNull();
    expect(within(tabs).getByRole("link", { name: "Today" })).toHaveAttribute("aria-current", "page");
  });

  it("shows only the page and the theme toggle on /login", () => {
    renderShell(null, "/login");
    expect(screen.getByRole("heading", { name: "Page" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(screen.getByRole("button", { name: "Dark theme" })).toBeInTheDocument();
  });

  it("signs out through the session endpoint, then reloads to /login", async () => {
    const fetchFn = vi.fn(async () => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchFn);
    renderShell("coach");
    fireEvent.click(screen.getAllByRole("button", { name: "Sign out" })[0]);
    await vi.waitFor(() => expect(browser.hardNavigate).toHaveBeenCalledWith("/login"));
    expect(fetchFn).toHaveBeenCalledWith("/api/cricai/_session", { method: "DELETE" });
  });

  it("still goes to /login when the sign-out call fails", async () => {
    browser.hardNavigate.mockReset();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("offline");
      }),
    );
    renderShell("parent");
    fireEvent.click(screen.getAllByRole("button", { name: "Sign out" })[0]);
    await vi.waitFor(() => expect(browser.hardNavigate).toHaveBeenCalledWith("/login"));
  });

  it("offers no sign-out when nobody is signed in", () => {
    renderShell(null);
    expect(screen.queryByRole("button", { name: "Sign out" })).toBeNull();
  });
});
