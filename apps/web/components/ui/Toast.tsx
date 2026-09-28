"use client";

import { X } from "lucide-react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { cn } from "@/lib/cn";
import type { BadgeTone } from "./Badge";

export type ToastTone = BadgeTone;

export interface ToastInput {
  title: string;
  description?: string;
  tone?: ToastTone;
}

interface ToastItem extends Required<Pick<ToastInput, "title" | "tone">> {
  id: number;
  description?: string;
}

interface ToastContextValue {
  toast: (input: ToastInput) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

/** How long a toast stays on screen. */
export const TOAST_DURATION_MS = 6000;

const TONES: Record<ToastTone, string> = {
  neutral: "border-border",
  success: "border-success",
  warning: "border-warning",
  danger: "border-danger",
  info: "border-info",
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(0);
  const timers = useRef(new Map<number, ReturnType<typeof setTimeout>>());

  const dismiss = useCallback((id: number) => {
    clearTimeout(timers.current.get(id));
    timers.current.delete(id);
    setItems((current) => current.filter((item) => item.id !== id));
  }, []);

  const toast = useCallback(
    ({ title, description, tone = "neutral" }: ToastInput) => {
      nextId.current += 1;
      const id = nextId.current;
      setItems((current) => [...current, { id, title, description, tone }]);
      timers.current.set(
        id,
        setTimeout(() => dismiss(id), TOAST_DURATION_MS),
      );
    },
    [dismiss],
  );

  useEffect(() => {
    const pending = timers.current;
    return () => pending.forEach((timer) => clearTimeout(timer));
  }, []);

  const value = useMemo(() => ({ toast }), [toast]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        role="status"
        aria-live="polite"
        aria-label="Notifications"
        data-print="hide"
        className="pointer-events-none fixed inset-x-0 bottom-20 z-50 flex flex-col items-center gap-2 px-4 lg:bottom-6 lg:items-end"
      >
        {items.map((item) => (
          <div
            key={item.id}
            data-tone={item.tone}
            className={cn(
              "pointer-events-auto flex w-full max-w-sm items-start gap-3 rounded-xl border-l-8 border-y border-r bg-surface-raised p-4 text-ink shadow-lg",
              TONES[item.tone],
            )}
          >
            <div className="flex-1">
              <p className="font-semibold">{item.title}</p>
              {item.description && <p className="text-ink-muted">{item.description}</p>}
            </div>
            <button
              type="button"
              aria-label={`Dismiss: ${item.title}`}
              onClick={() => dismiss(item.id)}
              className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-lg text-ink-muted hover:bg-surface"
            >
              <X aria-hidden="true" className="size-5" />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const context = useContext(ToastContext);
  if (context === null) {
    throw new Error("useToast() must be used inside <ToastProvider> (AppShell provides it)");
  }
  return context;
}
