/**
 * Ball lists under the pitch map (US-K2): the selected cell's balls, plus the
 * honest list of balls with NO bounce point (beamer/full toss, ball out of
 * frame, or simply unmarked — T7). Every ball links into the session
 * timeline at /sessions/{id}?ball=N (URL contract with the timeline story).
 */

import Link from "next/link";
import { Badge, Card, CardBody, CardHeader, CardTitle } from "@/components/ui";
import type { BouncePoint } from "./api";
import type { CellKeyPair } from "./PitchMapSvg";

interface BallListProps {
  sessionId: string;
  points: BouncePoint[];
  flaggedBalls: number[];
  noBounceBalls: number[];
  selectedCell: CellKeyPair | null;
}

function ballHref(sessionId: string, ballNo: number): string {
  return `/sessions/${sessionId}?ball=${ballNo}`;
}

const BALL_LINK =
  "inline-flex min-h-11 items-center font-semibold text-accent underline underline-offset-4";

export default function BallList({
  sessionId,
  points,
  flaggedBalls,
  noBounceBalls,
  selectedCell,
}: BallListProps) {
  const flagged = new Set(flaggedBalls);
  const cellPoints =
    selectedCell === null
      ? []
      : points.filter(
          (point) => point.line === selectedCell.line && point.length === selectedCell.length,
        );

  return (
    <section aria-label="ball lists" className="flex flex-col gap-6">
      <Card>
        <CardHeader>
          <CardTitle>
            {selectedCell === null
              ? "Balls in a zone"
              : `Balls in ${selectedCell.line} / ${selectedCell.length}`}
          </CardTitle>
        </CardHeader>
        <CardBody>
          {selectedCell === null ? (
            <p data-testid="no-cell-selected" className="text-ink-muted">
              Select a zone cell to list its balls.
            </p>
          ) : (
            <div data-testid="cell-ball-list">
              {cellPoints.length === 0 ? (
                <p data-testid="cell-empty" className="text-ink-muted">
                  No balls in this cell.
                </p>
              ) : (
                <ul className="flex flex-col divide-y divide-border">
                  {cellPoints.map((point) => (
                    <li key={point.ball_no} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-1">
                      <Link
                        data-testid={`ball-link-${point.ball_no}`}
                        href={ballHref(sessionId, point.ball_no)}
                        className={BALL_LINK}
                      >
                        {`ball ${point.ball_no}`}
                      </Link>
                      <span className="sr-only">
                        {` — ${point.source}${point.hollow ? ", low confidence" : ""}${
                          flagged.has(point.ball_no) ? ", flagged for review" : ""
                        }`}
                      </span>
                      <span aria-hidden="true" className="flex flex-wrap gap-2">
                        <Badge tone="neutral">{point.source}</Badge>
                        {point.hollow && <Badge tone="warning">low confidence</Badge>}
                        {flagged.has(point.ball_no) && <Badge tone="warning">flagged for review</Badge>}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </CardBody>
      </Card>

      {noBounceBalls.length > 0 && (
        <Card>
          <div data-testid="no-bounce-list">
            <CardHeader>
              <CardTitle>
                {`${noBounceBalls.length} ball${noBounceBalls.length === 1 ? "" : "s"} without a bounce point`}
              </CardTitle>
            </CardHeader>
            <CardBody>
              <p className="text-ink-muted">
                Full toss/beamer, ball out of frame, or not yet marked — not on the map.
              </p>
              <ul className="mt-2 flex flex-wrap gap-x-4">
                {noBounceBalls.map((ballNo) => (
                  <li key={ballNo}>
                    <Link
                      data-testid={`no-bounce-link-${ballNo}`}
                      href={ballHref(sessionId, ballNo)}
                      className={BALL_LINK}
                    >
                      {`ball ${ballNo}`}
                    </Link>
                  </li>
                ))}
              </ul>
            </CardBody>
          </div>
        </Card>
      )}
    </section>
  );
}
