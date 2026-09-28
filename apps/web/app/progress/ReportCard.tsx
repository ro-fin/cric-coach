/**
 * Weekly/monthly report card (US-G5/K4): renders the pinned contract-#3
 * body keys verbatim — period, positive line, honesty banner, milestones.
 * Numbers come from the body only (US-K4 data-parity). The honesty banner is
 * the API's own words, shown in the degraded banner unchanged.
 */

import { Badge, Card, CardBody, CardHeader, CardTitle, DegradedBanner } from "@/components/ui";
import { COPY } from "./copy";
import type { RollupReport } from "./types";

export function ReportCard({ report }: { report: RollupReport }) {
  const body = report.body;
  const titleId = `report-${report.id}-title`;
  return (
    <article data-testid={`report-${report.kind}`} data-status={report.status}>
      <Card aria-labelledby={titleId}>
        <CardHeader>
          <CardTitle id={titleId} className="capitalize">
            {`${report.kind} report`}
          </CardTitle>
          <Badge tone={report.status === "published" ? "success" : "neutral"}>
            {report.status}
          </Badge>
        </CardHeader>
        <CardBody className="flex flex-col gap-3">
          <p className="text-ink-muted">
            {body.period.start} {COPY.reportPeriodJoiner} {body.period.end}
          </p>
          {body.honesty_banner !== null && (
            <div data-testid="honesty-banner">
              <DegradedBanner reasons={[body.honesty_banner]} />
            </div>
          )}
          <p className="text-lg text-ink">{body.positive}</p>
          {body.milestones.length > 0 && (
            <ul data-testid="body-milestones" className="list-disc pl-6 text-ink">
              {body.milestones.map((milestone) => (
                <li key={`${milestone.kind}-${milestone.metric}-${milestone.achieved_on}`}>
                  {milestone.kind}: {milestone.metric} {milestone.value} {COPY.milestoneOn}{" "}
                  {milestone.achieved_on}
                </li>
              ))}
            </ul>
          )}
        </CardBody>
      </Card>
    </article>
  );
}
