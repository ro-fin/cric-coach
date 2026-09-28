import type { ReactNode } from "react";

export interface PageHeaderProps {
  title: string;
  description?: string;
  actions?: ReactNode;
}

export function PageHeader({ title, description, actions }: PageHeaderProps) {
  return (
    <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 className="text-3xl font-bold text-ink">{title}</h1>
        {description && <p className="mt-1 max-w-prose text-ink-muted">{description}</p>}
      </div>
      {actions && (
        <div data-print="hide" className="flex flex-wrap gap-2">
          {actions}
        </div>
      )}
    </header>
  );
}
