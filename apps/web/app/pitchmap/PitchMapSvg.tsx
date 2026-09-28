/**
 * SVG pitch map (US-K2): zone cells filled with their length-zone token and
 * shaded by density or control, bounce points to physical scale, declared
 * target-zone outlines, both end frames, handedness-mirrored cell geometry.
 * Every number shown comes from the API; every colour comes from a token
 * class, so light, dark and print follow the design system.
 */

import { cn } from "@/lib/cn";
import type { BouncePoint, BowlingTarget, HeatmapCell } from "./api";
import {
  cellRect,
  densityT,
  type EndFrame,
  HALF_WIDTH_M,
  type HandednessKey,
  LENGTHS,
  type LengthKey,
  LINES,
  type LineKey,
  MARGIN,
  NO_DATA_CLASS,
  offSideIsRight,
  PITCH_LENGTH_M,
  POPPING_CREASE_OFFSET_M,
  project,
  shadeOpacity,
  SVG_HEIGHT,
  SVG_WIDTH,
  zoneFillClass,
} from "./geometry";

export interface CellKeyPair {
  line: LineKey;
  length: LengthKey;
}

const PITCH_WIDTH_PX = SVG_WIDTH - 2 * MARGIN;
const PITCH_LENGTH_PX = SVG_HEIGHT - 2 * MARGIN;

interface PitchMapSvgProps {
  cells: HeatmapCell[];
  points: BouncePoint[];
  flaggedBalls: number[];
  handedness: HandednessKey;
  frame: EndFrame;
  showBouncePoints: boolean;
  colorByControl: boolean;
  targets: BowlingTarget[];
  showTargets: boolean;
  selectedCell: CellKeyPair | null;
  onSelectCell: (cell: CellKeyPair) => void;
}

interface CellShade {
  className: string;
  opacity: number;
}

/** Zone token for the band; opacity carries the API's density or control %.
 * Control shading is unknowable without tagged balls: neutral, never 0%. */
function cellShade(
  length: LengthKey,
  cell: HeatmapCell | undefined,
  peak: number,
  colorByControl: boolean,
): CellShade {
  if (colorByControl) {
    if (cell === undefined || cell.control_pct === null) {
      return { className: NO_DATA_CLASS, opacity: 1 };
    }
    return { className: zoneFillClass(length), opacity: shadeOpacity(cell.control_pct / 100) };
  }
  const balls = cell === undefined ? 0 : cell.balls;
  return { className: zoneFillClass(length), opacity: shadeOpacity(densityT(balls, peak)) };
}

function creaseLines(frame: EndFrame): { x1: number; y1: number; x2: number; y2: number }[] {
  const lines: { x1: number; y1: number; x2: number; y2: number }[] = [];
  for (const stumpsX of [0, PITCH_LENGTH_M]) {
    const towardMiddle = stumpsX === 0 ? 1 : -1;
    const poppingX = stumpsX + towardMiddle * POPPING_CREASE_OFFSET_M;
    const stumps = [project(frame, stumpsX, -HALF_WIDTH_M), project(frame, stumpsX, HALF_WIDTH_M)];
    const popping = [project(frame, poppingX, -HALF_WIDTH_M), project(frame, poppingX, HALF_WIDTH_M)];
    lines.push({ x1: stumps[0].u, y1: stumps[0].v, x2: stumps[1].u, y2: stumps[1].v });
    lines.push({ x1: popping[0].u, y1: popping[0].v, x2: popping[1].u, y2: popping[1].v });
  }
  return lines;
}

/** Bounce point styling: hollow = low confidence, amber ring = flagged. */
export function pointClass(hollow: boolean, flagged: boolean): string {
  const fill = hollow ? "fill-none" : "fill-ink";
  if (flagged) {
    return cn(fill, "stroke-warning");
  }
  return cn(fill, hollow ? "stroke-ink" : "stroke-surface");
}

export default function PitchMapSvg({
  cells,
  points,
  flaggedBalls,
  handedness,
  frame,
  showBouncePoints,
  colorByControl,
  targets,
  showTargets,
  selectedCell,
  onSelectCell,
}: PitchMapSvgProps) {
  const byKey = new Map(cells.map((cell) => [`${cell.line}|${cell.length}`, cell]));
  const peak = Math.max(0, ...cells.map((cell) => cell.balls));
  const flagged = new Set(flaggedBalls);
  const bowlerEndFar = frame === "batting_end";
  const offRight = offSideIsRight(frame, handedness);
  const offLabelX = offRight ? SVG_WIDTH - MARGIN / 4 : MARGIN / 4;

  return (
    <svg
      role="group"
      aria-label={`pitch map, ${frame === "batting_end" ? "batting" : "bowling"} end view`}
      viewBox={`0 0 ${SVG_WIDTH} ${SVG_HEIGHT}`}
      className="h-auto w-full max-w-md overflow-visible"
      data-testid="pitch-map-svg"
    >
      <rect
        x={MARGIN}
        y={MARGIN}
        width={PITCH_WIDTH_PX}
        height={PITCH_LENGTH_PX}
        className="fill-surface"
        pointerEvents="none"
      />
      {LINES.flatMap((line) =>
        LENGTHS.map((length) => {
          const rect = cellRect(frame, handedness, line, length);
          const cell = byKey.get(`${line}|${length}`);
          const balls = cell === undefined ? 0 : cell.balls;
          const selected =
            selectedCell !== null && selectedCell.line === line && selectedCell.length === length;
          const shade = cellShade(length, cell, peak, colorByControl);
          return (
            <g key={`${line}-${length}`}>
              <rect
                data-testid={`cell-${line}-${length}`}
                data-selected={selected}
                x={rect.u}
                y={rect.v}
                width={rect.w}
                height={rect.h}
                fillOpacity={shade.opacity}
                strokeWidth={selected ? 3 : 0.6}
                className={cn(
                  shade.className,
                  selected ? "stroke-ink" : "stroke-surface",
                  "cursor-pointer outline-none focus-visible:stroke-accent",
                )}
                tabIndex={0}
                role="button"
                aria-pressed={selected}
                aria-label={`${line} ${length}: ${balls} balls`}
                onClick={() => onSelectCell({ line, length })}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onSelectCell({ line, length });
                  }
                }}
              />
              <text
                data-testid={`count-${line}-${length}`}
                x={rect.u + rect.w / 2}
                y={rect.v + rect.h / 2}
                textAnchor="middle"
                dominantBaseline="central"
                fontSize={11}
                fontWeight={600}
                strokeWidth={3}
                paintOrder="stroke"
                className="fill-ink stroke-surface"
                pointerEvents="none"
              >
                {balls}
              </text>
            </g>
          );
        }),
      )}

      <rect
        x={MARGIN}
        y={MARGIN}
        width={PITCH_WIDTH_PX}
        height={PITCH_LENGTH_PX}
        fill="none"
        strokeWidth={1.2}
        className="stroke-ink"
        pointerEvents="none"
      />
      {creaseLines(frame).map((line, index) => (
        <line
          key={`crease-${index}`}
          x1={line.x1}
          y1={line.y1}
          x2={line.x2}
          y2={line.y2}
          strokeWidth={1}
          className="stroke-ink"
          pointerEvents="none"
        />
      ))}

      {showTargets &&
        targets.map((target, index) => {
          const rect = cellRect(
            frame,
            handedness,
            target.line as LineKey,
            target.length as LengthKey,
          );
          return (
            <rect
              key={`target-${index}`}
              data-testid={`target-${target.line}-${target.length}`}
              x={rect.u}
              y={rect.v}
              width={rect.w}
              height={rect.h}
              fill="none"
              strokeWidth={2.5}
              strokeDasharray="6 3"
              className="stroke-danger"
              pointerEvents="none"
            >
              <title>{`target: ${target.description}`}</title>
            </rect>
          );
        })}

      {showBouncePoints &&
        points.map((point) => {
          // Never clamped and never clipped (svg overflow stays visible):
          // an off-pitch bounce renders at its true spot, like the PNG.
          const at = project(frame, point.pitch_x, point.pitch_y);
          const isFlagged = flagged.has(point.ball_no);
          return (
            <circle
              key={`ball-${point.ball_no}`}
              data-testid={`point-${point.ball_no}`}
              data-hollow={point.hollow}
              data-flagged={isFlagged}
              cx={at.u}
              cy={at.v}
              r={4}
              strokeWidth={point.hollow || isFlagged ? 2 : 1}
              className={pointClass(point.hollow, isFlagged)}
              pointerEvents="none"
            >
              <title>{`ball ${point.ball_no} (${point.line} ${point.length}, ${point.source})`}</title>
            </circle>
          );
        })}

      <text
        x={SVG_WIDTH / 2}
        y={MARGIN / 2}
        textAnchor="middle"
        fontSize={12}
        className="fill-ink-muted"
      >
        {bowlerEndFar ? "bowler end (far)" : "batter end (far)"}
      </text>
      <text
        x={SVG_WIDTH / 2}
        y={SVG_HEIGHT - MARGIN / 4}
        textAnchor="middle"
        fontSize={12}
        className="fill-ink-muted"
      >
        {bowlerEndFar ? "batter end (near)" : "bowler end (near)"}
      </text>
      <text
        data-testid="off-side-label"
        x={offLabelX}
        y={SVG_HEIGHT / 2}
        textAnchor="middle"
        fontSize={11}
        className="fill-ink-muted"
        transform={`rotate(${offRight ? 90 : -90} ${offLabelX} ${SVG_HEIGHT / 2})`}
      >
        off side
      </text>
    </svg>
  );
}
