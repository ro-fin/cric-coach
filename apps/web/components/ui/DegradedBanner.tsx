import { CircleAlert } from "lucide-react";

export interface DegradedBannerProps {
  /** Reasons from the API (degraded, missing_views, a metric reason), shown verbatim. */
  reasons: string[];
}

export function DegradedBanner({ reasons }: DegradedBannerProps) {
  if (reasons.length === 0) {
    return null;
  }
  return (
    <section
      aria-label="Incomplete data"
      data-print="keep"
      className="rounded-xl border-2 border-warning bg-surface px-5 py-4 text-ink"
    >
      <p className="flex items-center gap-2 font-semibold text-warning">
        <CircleAlert aria-hidden="true" className="size-5 shrink-0" />
        Some of this data is incomplete
      </p>
      <ul className="mt-2 list-disc pl-6">
        {reasons.map((reason, index) => (
          <li key={`${index}-${reason}`}>{reason}</li>
        ))}
      </ul>
    </section>
  );
}
