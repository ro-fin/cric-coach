import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { expectNoA11yViolations } from "@/test/axe";
import { THEME_STORAGE_KEY, ThemeToggle } from "./ThemeToggle";

function mockSystemDark(dark: boolean | undefined) {
  vi.stubGlobal(
    "matchMedia",
    dark === undefined ? undefined : vi.fn(() => ({ matches: dark }) as MediaQueryList),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.localStorage.clear();
  delete document.documentElement.dataset.theme;
});

describe("ThemeToggle", () => {
  it("starts from the system preference and toggles, remembering the choice", async () => {
    mockSystemDark(true);
    const { container } = render(<ThemeToggle />);
    const button = screen.getByRole("button", { name: "Dark theme" });
    expect(button).toHaveAttribute("aria-pressed", "true");
    expect(document.documentElement.dataset.theme).toBe("dark");
    await expectNoA11yViolations(container);
    fireEvent.click(button);
    expect(button).toHaveAttribute("aria-pressed", "false");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
    fireEvent.click(button);
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("prefers the stored choice over the system", () => {
    mockSystemDark(true);
    window.localStorage.setItem(THEME_STORAGE_KEY, "light");
    render(<ThemeToggle />);
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  it("falls back to light without matchMedia and ignores junk in storage", () => {
    mockSystemDark(undefined);
    window.localStorage.setItem(THEME_STORAGE_KEY, "sepia");
    render(<ThemeToggle />);
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  it("uses a light system preference", () => {
    mockSystemDark(false);
    render(<ThemeToggle />);
    expect(screen.getByRole("button")).toHaveAttribute("aria-pressed", "false");
  });

  it("still toggles when storage is blocked", () => {
    mockSystemDark(false);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    render(<ThemeToggle />);
    fireEvent.click(screen.getByRole("button"));
    expect(document.documentElement.dataset.theme).toBe("dark");
  });
});
