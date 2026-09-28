/**
 * Workload safety panel (US-K4): rolling-7 overs vs ceiling, bowling-day
 * pattern and wellness flags — sticky, so it is never below the fold.
 *
 * Every number is the API's own (US-H1 summary / US-H4 state); nothing is
 * computed here (US-K4 data-parity).
 */

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
    <section
      aria-label="workload panel"
      data-testid="workload-panel"
      style={{ position: "sticky", top: 0 }}
    >
      <h2>{COPY.workloadTitle}</h2>
      {window === null ? (
        <p data-testid="workload-empty">{COPY.noWorkloadData}</p>
      ) : (
        <>
          <p data-testid="overs">
            {window.weighted_overs} {COPY.oversLabel}
            {window.ceiling_overs === null
              ? ` - ${COPY.noCeiling}`
              : ` ${COPY.oversCeilingJoiner} ${window.ceiling_overs}.`}
          </p>
          {window.remaining_balls !== null && (
            <p data-testid="remaining">
              {window.remaining_balls} {COPY.remainingSuffix}
            </p>
          )}
          <p data-testid="bowling-days">
            {COPY.bowlingDaysLabel}:{" "}
            {window.bowling_days.length > 0
              ? window.bowling_days.join(", ")
              : COPY.noBowlingDays}
          </p>
          {window.violations.length > 0 ? (
            <ul data-testid="violations">
              {window.violations.map((violation) => (
                <li key={violation}>{violation}</li>
              ))}
            </ul>
          ) : (
            <p data-testid="no-violations">{COPY.noWorkloadFlags}</p>
          )}
        </>
      )}
      <p data-testid="wellness">{wellness.pain_active ? COPY.wellnessFlag : COPY.wellnessOk}</p>
    </section>
  );
}
