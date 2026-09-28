/**
 * Hand-rolled SVG trend chart (US-K4): per-point n labels, confidence
 * shading for qualified series, an honest note when the series is not.
 *
 * No charting deps, no client-side metric computation: every number shown is
 * a value the API returned, rendered verbatim (US-K4 data-parity). Geometry
 * (pixel coordinates) is presentation, not data.
 */

import { COPY } from "./copy";
import type { TrendPoint, TrendSeries } from "./types";

export const CHART_WIDTH = 320;
export const CHART_HEIGHT = 140;
export const CHART_PAD = 24;

export type PlacedPoint = {
  x: number;
  y: number;
  point: TrendPoint;
};

/** Evenly spaced x, min/max-scaled y; a flat or single-point series centres. */
export function placePoints(points: TrendPoint[]): PlacedPoint[] {
  const values = points.map((point) => point.value);
  const low = Math.min(...values);
  const high = Math.max(...values);
  const span = high - low;
  const innerWidth = CHART_WIDTH - 2 * CHART_PAD;
  const innerHeight = CHART_HEIGHT - 2 * CHART_PAD;
  const step = points.length > 1 ? innerWidth / (points.length - 1) : 0;
  return points.map((point, index) => ({
    x: CHART_PAD + index * step,
    y:
      span > 0
        ? CHART_PAD + innerHeight * (1 - (point.value - low) / span)
        : CHART_HEIGHT / 2,
    point,
  }));
}

function polyline(placed: PlacedPoint[]): string {
  return placed.map(({ x, y }) => `${x},${y}`).join(" ");
}

function shadePolygon(placed: PlacedPoint[]): string {
  const first = placed[0];
  const last = placed[placed.length - 1];
  const floor = CHART_HEIGHT - CHART_PAD;
  return `${first.x},${floor} ${polyline(placed)} ${last.x},${floor}`;
}

export function TrendChart({ trend }: { trend: TrendSeries }) {
  if (trend.points.length === 0) {
    return null; // a series exists only once a baseline point exists
  }
  const placed = placePoints(trend.points);
  return (
    <figure
      aria-label={`trend ${trend.metric} ${trend.zone_key}`}
      data-testid={`trend-${trend.metric}-${trend.zone_key}`}
    >
      <figcaption>
        <span>{trend.metric}</span> <span>({trend.zone_key})</span>{" "}
        {/* US-G5 guard: a direction verdict needs a qualified series (>= 3
            sessions, >= 30 balls/point) — an unqualified one says so instead. */}
        <span
          data-testid="direction"
          data-direction={trend.qualified ? trend.direction : "unqualified"}
        >
          {trend.qualified ? trend.direction : COPY.unqualifiedDirection}
        </span>
      </figcaption>
      <svg
        role="img"
        viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
        width={CHART_WIDTH}
        height={CHART_HEIGHT}
      >
        {trend.qualified && (
          <polygon
            data-testid="confidence-shade"
            points={shadePolygon(placed)}
            fill="currentColor"
            opacity={0.12}
          />
        )}
        <polyline
          points={polyline(placed)}
          fill="none"
          stroke="currentColor"
          strokeDasharray={trend.qualified ? undefined : "4 3"}
        />
        {placed.map(({ x, y, point }) => (
          <g key={point.date}>
            <circle cx={x} cy={y} r={3} />
            <text x={x} y={y - 8} fontSize={9} textAnchor="middle">
              {point.value}
            </text>
            <text x={x} y={CHART_HEIGHT - 6} fontSize={8} textAnchor="middle">
              n={point.n}
            </text>
          </g>
        ))}
      </svg>
      {!trend.qualified && <p data-testid="unqualified-note">{COPY.unqualifiedNote}</p>}
    </figure>
  );
}
