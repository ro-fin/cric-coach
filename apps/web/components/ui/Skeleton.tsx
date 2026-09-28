import { cn } from "@/lib/cn";

export interface SkeletonProps {
  /** How many placeholder bars to draw. */
  lines?: number;
  /** What is loading, announced to screen readers (e.g. "Loading sessions"). */
  label: string;
  className?: string;
}

export function Skeleton({ lines = 3, label, className }: SkeletonProps) {
  return (
    <div role="status" aria-busy="true" className={cn("flex flex-col gap-3", className)}>
      <span className="sr-only">{label}</span>
      {Array.from({ length: lines }, (_, index) => (
        <div
          key={index}
          aria-hidden="true"
          data-testid="skeleton-line"
          className={cn(
            "h-5 animate-pulse rounded-md bg-border",
            index === lines - 1 && lines > 1 ? "w-2/3" : "w-full",
          )}
        />
      ))}
    </div>
  );
}
