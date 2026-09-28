"use client";

/**
 * Per-ball metric summary chips (US-K1). Fetches the selected ball's stored
 * phase metrics from the ball_metrics router and renders them verbatim —
 * values, units, confidences and null-reasons come from the server
 * (server-data-faithful: no client-side metric computation).
 */

import { useEffect, useMemo, useState } from "react";
import { Badge, ErrorState, Skeleton } from "@/components/ui";
import { createApiClient, defaultConfig } from "@/lib/api";
import type { ApiClient, PhaseMetricsOut } from "@/lib/api";
import { confidenceLabel, formatMetricValue } from "./metrics";

export interface BallMetricsPanelProps {
  sessionId: string;
  ballNo: number;
  client?: ApiClient;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export default function BallMetricsPanel({ sessionId, ballNo, client }: BallMetricsPanelProps) {
  const api = useMemo(() => client ?? createApiClient(defaultConfig()), [client]);
  const [phases, setPhases] = useState<PhaseMetricsOut[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setPhases(null);
    setError(null);
    api.ballMetrics(sessionId, ballNo).then(
      (result) => {
        if (!cancelled) {
          setPhases(result);
        }
      },
      (reason: unknown) => {
        if (!cancelled) {
          setError(errorMessage(reason));
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [api, sessionId, ballNo, attempt]);

  if (error !== null) {
    return (
      <div data-testid="metrics-error">
        <ErrorState
          message={`Metrics unavailable: ${error}`}
          onRetry={() => setAttempt((n) => n + 1)}
        />
      </div>
    );
  }
  if (phases === null) {
    return (
      <div data-testid="metrics-loading">
        <Skeleton label={`Loading metrics for ball ${ballNo}`} lines={2} />
      </div>
    );
  }
  if (phases.length === 0) {
    return (
      <p data-testid="metrics-empty" className="text-ink-muted">
        No metrics recorded for ball {ballNo}.
      </p>
    );
  }
  return (
    <section aria-label={`metrics for ball ${ballNo}`} className="grid gap-3 md:grid-cols-2">
      {phases.map((phase) => (
        <article key={phase.phase} className="rounded-xl border border-border p-4">
          <h3 className="mb-2 font-semibold">
            {phase.phase} {phase.stored ? "(stored)" : "(synthesized from manual sources)"}
          </h3>
          {!phase.stored && (
            <Badge tone="warning" className="mb-2">
              not from the vision pipeline
            </Badge>
          )}
          <ul className="flex flex-col gap-1 text-sm">
            {Object.keys(phase.metrics)
              .sort()
              .map((name) => {
                const metric = phase.metrics[name];
                return (
                  <li key={name}>
                    {name}: {formatMetricValue(metric)} · {confidenceLabel(metric)}
                    {metric.proxy === true ? " · proxy" : ""}
                    {typeof metric.source === "string" ? ` · ${metric.source}` : ""}
                  </li>
                );
              })}
          </ul>
        </article>
      ))}
    </section>
  );
}
