/**
 * Milestone feed (US-K4/G5): the celebratory surface.
 *
 * Parent view shows the exact metric/value/date the API returned (data
 * parity); kid mode swaps in age-appropriate celebration copy — the streak
 * count is the only number kept (an attendance cheer, never workload).
 */

import { Award, Flame, Trophy } from "lucide-react";
import { Badge, type BadgeTone, EmptyState } from "@/components/ui";
import { COPY } from "./copy";
import type { Milestone, MilestoneKind } from "./types";

export function kidLine(milestone: Milestone): string {
  if (milestone.kind === "personal_best") {
    return COPY.kidPersonalBest;
  }
  if (milestone.kind === "volume") {
    return COPY.kidVolume;
  }
  return `${milestone.value} ${COPY.kidStreakSuffix}`;
}

const KIND_TONES: Readonly<Record<MilestoneKind, BadgeTone>> = {
  personal_best: "success",
  volume: "info",
  streak: "warning",
};

const KIND_ICONS = { personal_best: Trophy, volume: Award, streak: Flame } as const;

export function MilestoneFeed({
  milestones,
  kidMode,
}: {
  milestones: Milestone[];
  kidMode: boolean;
}) {
  if (milestones.length === 0) {
    return (
      <div data-testid="milestones-empty">
        <EmptyState title={COPY.emptyMilestones} />
      </div>
    );
  }
  return (
    <ul
      aria-label="milestone feed"
      data-testid="milestone-feed"
      className="flex flex-col divide-y divide-border"
    >
      {milestones.map((milestone) => {
        const Icon = KIND_ICONS[milestone.kind];
        return (
          <li
            key={milestone.id}
            data-kind={milestone.kind}
            className="flex flex-wrap items-center gap-3 py-3"
          >
            <Icon aria-hidden="true" className="size-6 shrink-0 text-accent" />
            <Badge tone={KIND_TONES[milestone.kind]}>{COPY.milestoneLabels[milestone.kind]}</Badge>{" "}
            {kidMode ? (
              <span className="text-lg text-ink">{kidLine(milestone)}</span>
            ) : (
              <span className="text-ink">
                {milestone.metric} {milestone.value} {COPY.milestoneOn} {milestone.achieved_on}
              </span>
            )}
          </li>
        );
      })}
    </ul>
  );
}
