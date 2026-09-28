/**
 * Weekly/monthly report card (US-G5/K4): renders the pinned contract-#3
 * body keys verbatim — period, positive line, honesty banner, milestones.
 * Numbers come from the body only (US-K4 data-parity).
 */

import type { RollupReport } from "./types";

export function ReportCard({ report }: { report: RollupReport }) {
  const body = report.body;
  return (
    <article
      aria-label={`${report.kind} report`}
      data-testid={`report-${report.kind}`}
      data-status={report.status}
    >
      <h3>{report.kind} report</h3>
      <p>
        {body.period.start} to {body.period.end}
      </p>
      {body.honesty_banner !== null && (
        <p data-testid="honesty-banner">{body.honesty_banner}</p>
      )}
      <p>{body.positive}</p>
      {body.milestones.length > 0 && (
        <ul data-testid="body-milestones">
          {body.milestones.map((milestone) => (
            <li key={`${milestone.kind}-${milestone.metric}-${milestone.achieved_on}`}>
              {milestone.kind}: {milestone.metric} {milestone.value} on {milestone.achieved_on}
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}
