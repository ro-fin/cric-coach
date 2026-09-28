"use client";

/**
 * Virtualized ball-by-ball timeline (US-K1, US-B5).
 *
 * Hand-rolled windowed rendering: only the rows near the viewport exist in
 * the DOM, so a 500-ball index renders within the < 2 s budget. Each chip is
 * colored by outcome/contact and clicking one seeks the player to that ball.
 * A ?ball=N deep link (US-K2 pitch-map click-through) scrolls the requested
 * ball into the rendered window once, on arrival.
 */

import { useEffect, useRef, useState } from "react";
import type { UIEvent } from "react";
import { chipSummary, chipTone, windowSlice } from "./timeline";
import type { BallRow } from "./timeline";

export const ROW_PX = 40;
export const OVERSCAN = 5;
export const DEFAULT_VIEWPORT_PX = 480;

export interface BallTimelineProps {
  rows: BallRow[];
  selectedBallNo: number | null;
  onSelect: (ballNo: number) => void;
  viewportPx?: number;
  /** Deep-linked ball to bring into view on arrival (US-K2 ?ball=N). */
  scrollToBallNo?: number;
}

export default function BallTimeline({
  rows,
  selectedBallNo,
  onSelect,
  viewportPx = DEFAULT_VIEWPORT_PX,
  scrollToBallNo,
}: BallTimelineProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const scrolledRef = useRef(false);
  const [scrollTop, setScrollTop] = useState(0);
  const slice = windowSlice(rows.length, scrollTop, viewportPx, ROW_PX, OVERSCAN);

  // One-shot deep-link scroll: position the window on the requested ball,
  // then never fight the user's own scrolling on later row changes.
  useEffect(() => {
    const node = scrollRef.current;
    if (scrolledRef.current || scrollToBallNo === undefined || node === null) {
      return;
    }
    const index = rows.findIndex((row) => row.ballNo === scrollToBallNo);
    if (index === -1) {
      return;
    }
    scrolledRef.current = true;
    const top = index * ROW_PX;
    node.scrollTop = top;
    setScrollTop(top);
  }, [rows, scrollToBallNo]);

  function handleScroll(event: UIEvent<HTMLDivElement>) {
    setScrollTop(event.currentTarget.scrollTop);
  }

  if (rows.length === 0) {
    return <p data-testid="timeline-empty">No balls match the current filters.</p>;
  }

  return (
    <div
      ref={scrollRef}
      data-testid="timeline-scroll"
      onScroll={handleScroll}
      style={{ height: viewportPx, overflowY: "auto" }}
    >
      <ul aria-label="ball timeline" style={{ listStyle: "none", margin: 0, padding: 0 }}>
        <li aria-hidden="true" style={{ height: slice.padTopPx }} />
        {rows.slice(slice.start, slice.end).map((row) => (
          <li key={row.ballNo} style={{ height: ROW_PX }}>
            <button
              type="button"
              data-tone={chipTone(row)}
              aria-pressed={row.ballNo === selectedBallNo}
              onClick={() => onSelect(row.ballNo)}
            >
              <span>Ball {row.ballNo}</span> <span>{chipSummary(row)}</span>{" "}
              <span>
                {row.event === null
                  ? "no event"
                  : `event ${row.event.source} · conf ${row.event.confidence.toFixed(2)}`}
              </span>{" "}
              <span>{row.clips.length === 0 ? "no clips" : `${row.clips.length} clips`}</span>
            </button>
          </li>
        ))}
        <li aria-hidden="true" style={{ height: slice.padBottomPx }} />
      </ul>
    </div>
  );
}
