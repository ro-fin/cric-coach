/**
 * Workload safety panel (US-K4): rolling-7 overs vs ceiling, bowling-day
 * pattern and wellness flags — sticky, so it is never below the fold.
 *
 * Every number is the API's own (US-H1 summary / US-H4 state), passed to the
 * StatTile as the served value; nothing is computed here (US-K4 data-parity).
 * A null ceiling or countdown shows its reason in words, never a dash.
 */

import { Badge, Card, CardBody, CardHeader, CardTitle, StatTile } from "@/components/ui";
import { COPY } from "./copy";
import type { WellnessState, WorkloadWindow } from "./types";

export function WorkloadPanel({
  window,
  wellness,
}: {
  window: WorkloadWindow | null;
  wellness: WellnessState;
}) {
  return (
    <div data-testid="workload-panel" className="sticky top-14 z-10 lg:top-4">
      <Card aria-labelledby="workload-title" className="shadow-md">
        <CardHeader>
          <CardTitle id="workload-title">{COPY.workloadTitle}</CardTitle>
          <span data-testid="wellness">
            {wellness.pain_active ? (
              <Badge tone="danger">{COPY.wellnessFlag}</Badge>
            ) : (
              <Badge tone="success">{COPY.wellnessOk}</Badge>
            )}
          </span>
        </CardHeader>
        <CardBody className="flex flex-col gap-4">
          {window === null ? (
            <p data-testid="workload-empty" className="text-ink-muted">
              {COPY.noWorkloadData}
            </p>
          ) : (
            <>
              <div className="grid gap-3 sm:grid-cols-3">
                <div data-testid="overs">
                  <StatTile
                    label={COPY.oversTile}
                    value={String(window.weighted_overs)}
                    unit={COPY.oversUnit}
                  />
                </div>
                <div data-testid="ceiling">
                  <StatTile
                    label={COPY.ceilingTile}
                    value={window.ceiling_overs === null ? null : String(window.ceiling_overs)}
                    unit={COPY.oversUnit}
                    reason={window.ceiling_overs === null ? COPY.noCeiling : null}
                  />
                </div>
                <div data-testid="remaining">
                  <StatTile
                    label={COPY.remainingTile}
                    value={window.remaining_balls === null ? null : String(window.remaining_balls)}
                    unit={COPY.ballsUnit}
                    reason={window.remaining_balls === null ? COPY.noRemaining : null}
                  />
                </div>
              </div>
              <p data-testid="bowling-days" className="text-ink">
                <span className="font-semibold">{COPY.bowlingDaysLabel}:</span>{" "}
                {window.bowling_days.length > 0
                  ? window.bowling_days.join(", ")
                  : COPY.noBowlingDays}
              </p>
              {window.violations.length > 0 ? (
                <div>
                  <p className="font-semibold text-danger">{COPY.flagsTitle}</p>
                  <ul data-testid="violations" className="mt-1 flex flex-wrap gap-2">
                    {window.violations.map((violation) => (
                      <li key={violation}>
                        <Badge tone="danger">{violation}</Badge>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : (
                <p data-testid="no-violations" className="text-ink-muted">
                  {COPY.noWorkloadFlags}
                </p>
              )}
            </>
          )}
        </CardBody>
      </Card>
    </div>
  );
}
