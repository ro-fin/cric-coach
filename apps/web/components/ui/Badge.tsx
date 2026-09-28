import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

export type BadgeTone = "neutral" | "success" | "warning" | "danger" | "info";

export interface BadgeProps {
  tone: BadgeTone;
  children: ReactNode;
  className?: string;
}

const TONES: Record<BadgeTone, string> = {
  neutral: "border-border text-ink-muted",
  success: "border-success text-success",
  warning: "border-warning text-warning",
  danger: "border-danger text-danger",
  info: "border-info text-info",
};

export function Badge({ tone, children, className }: BadgeProps) {
  return (
    <span
      data-tone={tone}
      className={cn(
        "inline-flex items-center gap-1 rounded-full border-2 bg-surface px-3 py-0.5 text-sm font-semibold",
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}
