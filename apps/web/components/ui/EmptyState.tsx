import { Inbox } from "lucide-react";
import type { ReactNode } from "react";

export interface EmptyStateProps {
  title: string;
  description?: string;
  /** The next thing the user can do (a button or link). */
  action?: ReactNode;
}

export function EmptyState({ title, description, action }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-xl border-2 border-dashed border-border px-6 py-10 text-center">
      <Inbox aria-hidden="true" className="size-10 text-ink-muted" />
      <h2 className="text-xl font-semibold text-ink">{title}</h2>
      {description && <p className="max-w-prose text-ink-muted">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}
