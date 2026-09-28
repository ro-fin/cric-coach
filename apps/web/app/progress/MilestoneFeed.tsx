/**
 * Milestone feed (US-K4/G5): the celebratory surface.
 *
 * Parent view shows the exact metric/value/date the API returned (data
 * parity); kid mode swaps in age-appropriate celebration copy — the streak
 * count is the only number kept (an attendance cheer, never workload).
 */

import { COPY } from "./copy";
import type { Milestone } from "./types";

export function kidLine(milestone: Milestone): string {
  if (milestone.kind === "personal_best") {
    return COPY.kidPersonalBest;
  }
  if (milestone.kind === "volume") {
    return COPY.kidVolume;
  }
  return `${milestone.value} ${COPY.kidStreakSuffix}`;
}

export function MilestoneFeed({
  milestones,
  kidMode,
}: {
  milestones: Milestone[];
  kidMode: boolean;
}) {
  if (milestones.length === 0) {
    return <p data-testid="milestones-empty">{COPY.emptyMilestones}</p>;
  }
  return (
    <ul aria-label="milestone feed" data-testid="milestone-feed">
      {milestones.map((milestone) => (
        <li key={milestone.id} data-kind={milestone.kind}>
          <strong>{COPY.milestoneLabels[milestone.kind]}</strong>{" "}
          {kidMode ? (
            <span>{kidLine(milestone)}</span>
          ) : (
            <span>
              {milestone.metric} {milestone.value} on {milestone.achieved_on}
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}
