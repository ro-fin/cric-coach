export interface StatTileProps {
  label: string;
  /** The API value, already formatted. null means not measured: the reason is shown instead. */
  value: string | null;
  unit?: string;
  /** API confidence in [0, 1]. */
  confidence?: number;
  /** API reason (why a value is missing or qualified), rendered verbatim. */
  reason?: string | null;
}

export function StatTile({ label, value, unit, confidence, reason }: StatTileProps) {
  return (
    <div
      data-print="keep"
      className="flex min-h-11 flex-col gap-1 rounded-xl border border-border bg-surface px-4 py-3"
    >
      <p className="text-sm font-semibold text-ink-muted">{label}</p>
      {value === null ? (
        <p className="text-base text-warning">{reason ?? "Not measured"}</p>
      ) : (
        <>
          <p className="text-3xl font-bold text-ink">
            {value}
            {unit && <span className="ml-1 text-base font-semibold text-ink-muted">{unit}</span>}
          </p>
          {reason && <p className="text-sm text-warning">{reason}</p>}
        </>
      )}
      {confidence !== undefined && (
        <p className="text-sm text-ink-muted">Confidence {Math.round(confidence * 100)}%</p>
      )}
    </div>
  );
}
