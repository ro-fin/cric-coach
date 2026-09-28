"use client";

import { Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";

export type Theme = "light" | "dark";

export const THEME_STORAGE_KEY = "cricai-theme";

function readStoredTheme(): Theme | null {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    return stored === "light" || stored === "dark" ? stored : null;
  } catch {
    return null;
  }
}

function systemTheme(): Theme {
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/**
 * Light/dark switch. The choice is pinned on <html data-theme> (which the
 * tokens in globals.css follow) and remembered on this device.
 */
export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("light");

  useEffect(() => {
    const initial = readStoredTheme() ?? systemTheme();
    setTheme(initial);
    document.documentElement.dataset.theme = initial;
  }, []);

  const toggle = () => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.dataset.theme = next;
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // storage blocked (private mode): the choice lasts for this page only
    }
  };

  const dark = theme === "dark";
  return (
    <button
      type="button"
      aria-pressed={dark}
      onClick={toggle}
      className="inline-flex min-h-11 min-w-11 items-center justify-center gap-2 rounded-lg px-3 text-ink hover:bg-surface-raised"
    >
      {dark ? (
        <Moon aria-hidden="true" className="size-5" />
      ) : (
        <Sun aria-hidden="true" className="size-5" />
      )}
      <span className="sr-only lg:not-sr-only">Dark theme</span>
    </button>
  );
}
