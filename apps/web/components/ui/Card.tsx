import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

interface SlotProps {
  children: ReactNode;
  className?: string;
}

export interface CardProps extends SlotProps {
  /** Names the card, making it a region landmark (use one of the two). */
  "aria-label"?: string;
  /** Id of the element (usually the CardTitle) that names the card. */
  "aria-labelledby"?: string;
}

export function Card({ children, className, ...named }: CardProps) {
  return (
    <section
      data-print="keep"
      {...named}
      className={cn("rounded-xl border border-border bg-surface text-ink shadow-sm", className)}
    >
      {children}
    </section>
  );
}

export function CardHeader({ children, className }: SlotProps) {
  return (
    <div className={cn("flex flex-wrap items-center justify-between gap-2 px-5 pt-5", className)}>
      {children}
    </div>
  );
}

export function CardTitle({ children, className, id }: SlotProps & { id?: string }) {
  return (
    <h2 id={id} className={cn("text-xl font-semibold text-ink", className)}>
      {children}
    </h2>
  );
}

export function CardBody({ children, className }: SlotProps) {
  return <div className={cn("px-5 py-4", className)}>{children}</div>;
}
