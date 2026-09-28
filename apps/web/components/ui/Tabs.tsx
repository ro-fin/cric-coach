"use client";

import {
  createContext,
  useContext,
  useId,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { cn } from "@/lib/cn";

interface TabsContextValue {
  baseId: string;
  value: string;
  select: (value: string) => void;
}

const TabsContext = createContext<TabsContextValue | null>(null);

function useTabs(component: string): TabsContextValue {
  const context = useContext(TabsContext);
  if (context === null) {
    throw new Error(`<${component}> must be used inside <Tabs>`);
  }
  return context;
}

/** Tab values become part of element ids, so keep them id-safe. */
function idPart(value: string): string {
  return value.replace(/[^A-Za-z0-9_-]/g, "_");
}

export interface TabsProps {
  /** Initially selected tab (uncontrolled). */
  defaultValue?: string;
  /** Selected tab (controlled); pair with onValueChange. */
  value?: string;
  onValueChange?: (value: string) => void;
  children: ReactNode;
  className?: string;
}

export function Tabs({ defaultValue = "", value, onValueChange, children, className }: TabsProps) {
  const baseId = useId();
  const [internal, setInternal] = useState(defaultValue);
  const current = value ?? internal;
  const select = (next: string) => {
    setInternal(next);
    onValueChange?.(next);
  };
  return (
    <TabsContext.Provider value={{ baseId, value: current, select }}>
      <div className={className}>{children}</div>
    </TabsContext.Provider>
  );
}

export interface TabListProps {
  /** Accessible name for the tab list. */
  label: string;
  children: ReactNode;
  className?: string;
}

const NEXT_KEYS: Record<string, (index: number, count: number) => number> = {
  ArrowRight: (index, count) => (index + 1) % count,
  ArrowDown: (index, count) => (index + 1) % count,
  ArrowLeft: (index, count) => (index - 1 + count) % count,
  ArrowUp: (index, count) => (index - 1 + count) % count,
  Home: () => 0,
  End: (_index, count) => count - 1,
};

export function TabList({ label, children, className }: TabListProps) {
  useTabs("TabList");
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const move = NEXT_KEYS[event.key];
    if (!move) {
      return;
    }
    const tabs = Array.from(
      event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]:not([disabled])'),
    );
    const index = tabs.findIndex((tab) => tab === document.activeElement);
    const next = tabs[move(Math.max(index, 0), tabs.length)];
    event.preventDefault();
    next.focus();
    next.click();
  };
  return (
    <div
      role="tablist"
      aria-label={label}
      onKeyDown={onKeyDown}
      className={cn("flex gap-1 overflow-x-auto border-b-2 border-border", className)}
    >
      {children}
    </div>
  );
}

export interface TabProps {
  value: string;
  children: ReactNode;
  disabled?: boolean;
}

export function Tab({ value, children, disabled }: TabProps) {
  const tabs = useTabs("Tab");
  const selected = tabs.value === value;
  return (
    <button
      type="button"
      role="tab"
      id={`${tabs.baseId}-tab-${idPart(value)}`}
      aria-selected={selected}
      aria-controls={`${tabs.baseId}-panel-${idPart(value)}`}
      tabIndex={selected ? 0 : -1}
      disabled={disabled}
      onClick={() => tabs.select(value)}
      className={cn(
        "-mb-0.5 min-h-11 whitespace-nowrap border-b-4 px-4 font-semibold disabled:opacity-50",
        selected ? "border-accent text-ink" : "border-transparent text-ink-muted hover:text-ink",
      )}
    >
      {children}
    </button>
  );
}

export interface TabPanelProps {
  value: string;
  children: ReactNode;
  className?: string;
}

export function TabPanel({ value, children, className }: TabPanelProps) {
  const tabs = useTabs("TabPanel");
  if (tabs.value !== value) {
    return null;
  }
  return (
    <div
      role="tabpanel"
      id={`${tabs.baseId}-panel-${idPart(value)}`}
      aria-labelledby={`${tabs.baseId}-tab-${idPart(value)}`}
      tabIndex={0}
      className={cn("py-4", className)}
    >
      {children}
    </div>
  );
}
