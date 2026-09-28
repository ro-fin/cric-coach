/**
 * Ball lists under the pitch map (US-K2): the selected cell's balls, plus the
 * honest list of balls with NO bounce point (beamer/full toss, ball out of
 * frame, or simply unmarked — T7). Every ball links into the session
 * timeline at /sessions/{id}?ball=N (URL contract with the timeline story).
 */

import Link from "next/link";
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
    <section aria-label="ball lists">
      {selectedCell === null ? (
        <p data-testid="no-cell-selected">Select a zone cell to list its balls.</p>
      ) : (
        <div data-testid="cell-ball-list">
          <h3>{`Balls in ${selectedCell.line} / ${selectedCell.length}`}</h3>
          {cellPoints.length === 0 ? (
            <p data-testid="cell-empty">No balls in this cell.</p>
          ) : (
            <ul>
              {cellPoints.map((point) => (
                <li key={point.ball_no}>
                  <Link
                    data-testid={`ball-link-${point.ball_no}`}
                    href={ballHref(sessionId, point.ball_no)}
                  >
                    {`ball ${point.ball_no}`}
                  </Link>
                  {` — ${point.source}${point.hollow ? ", low confidence" : ""}${
                    flagged.has(point.ball_no) ? ", flagged for review" : ""
                  }`}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {noBounceBalls.length > 0 && (
        <div data-testid="no-bounce-list">
          <h3>{`${noBounceBalls.length} ball${noBounceBalls.length === 1 ? "" : "s"} without a bounce point`}</h3>
          <p>Full toss/beamer, ball out of frame, or not yet marked — not on the map.</p>
          <ul>
            {noBounceBalls.map((ballNo) => (
              <li key={ballNo}>
                <Link
                  data-testid={`no-bounce-link-${ballNo}`}
                  href={ballHref(sessionId, ballNo)}
                >
                  {`ball ${ballNo}`}
                </Link>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
