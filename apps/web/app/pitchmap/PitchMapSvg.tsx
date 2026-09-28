/**
 * SVG pitch map (US-K2): zone cells shaded by density or control, bounce
 * points to physical scale, declared target-zone outlines, both end frames,
 * handedness-mirrored cell geometry. Every number shown comes from the API.
 */

import type { BouncePoint, BowlingTarget, HeatmapCell } from "./api";
import {
  cellRect,
  densityT,
  type EndFrame,
  HALF_WIDTH_M,
  type HandednessKey,
  labelColor,
  LENGTHS,
  type LengthKey,
  LINES,
  type LineKey,
  MARGIN,
  NO_DATA_FILL,
  offSideIsRight,
  PITCH_LENGTH_M,
  POPPING_CREASE_OFFSET_M,
  project,
  SVG_HEIGHT,
  SVG_WIDTH,
  viridis,
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

function cellFill(cell: HeatmapCell | undefined, peak: number, colorByControl: boolean): string {
  if (colorByControl) {
    // Control shading is unknowable without tagged balls: neutral, never 0%.
    if (cell === undefined || cell.control_pct === null) {
      return NO_DATA_FILL;
    }
    return viridis(cell.control_pct / 100);
  }
  return viridis(densityT(cell === undefined ? 0 : cell.balls, peak));
}

function cellLabelColor(
  cell: HeatmapCell | undefined,
  peak: number,
  colorByControl: boolean,
): string {
  if (colorByControl) {
    if (cell === undefined || cell.control_pct === null) {
      return "#000000";
    }
    return labelColor(cell.control_pct / 100);
  }
  return labelColor(densityT(cell === undefined ? 0 : cell.balls, peak));
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
  const pitchTopLeft = {
    u: MARGIN,
    v: MARGIN,
  };

  return (
    <svg
      role="img"
      aria-label={`pitch map, ${frame === "batting_end" ? "batting" : "bowling"} end view`}
      width={SVG_WIDTH}
      height={SVG_HEIGHT}
      viewBox={`0 0 ${SVG_WIDTH} ${SVG_HEIGHT}`}
      style={{ overflow: "visible" }}
      data-testid="pitch-map-svg"
    >
      {LINES.flatMap((line) =>
        LENGTHS.map((length) => {
          const rect = cellRect(frame, handedness, line, length);
          const cell = byKey.get(`${line}|${length}`);
          const balls = cell === undefined ? 0 : cell.balls;
          const selected =
            selectedCell !== null && selectedCell.line === line && selectedCell.length === length;
          return (
            <g key={`${line}-${length}`}>
              <rect
                data-testid={`cell-${line}-${length}`}
                x={rect.u}
                y={rect.v}
                width={rect.w}
                height={rect.h}
                fill={cellFill(cell, peak, colorByControl)}
                stroke={selected ? "#111827" : "#ffffff"}
                strokeWidth={selected ? 3 : 0.6}
                tabIndex={0}
                role="button"
                aria-label={`${line} ${length}: ${balls} balls`}
                onClick={() => onSelectCell({ line, length })}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
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
                fill={cellLabelColor(cell, peak, colorByControl)}
                pointerEvents="none"
              >
                {balls}
              </text>
            </g>
          );
        }),
      )}

      <rect
        x={pitchTopLeft.u}
        y={pitchTopLeft.v}
        width={PITCH_WIDTH_PX}
        height={PITCH_LENGTH_PX}
        fill="none"
        stroke="#111827"
        strokeWidth={1.2}
        pointerEvents="none"
      />
      {creaseLines(frame).map((line, index) => (
        <line
          key={`crease-${index}`}
          x1={line.x1}
          y1={line.y1}
          x2={line.x2}
          y2={line.y2}
          stroke="#111827"
          strokeWidth={1}
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
              stroke="#dc2626"
              strokeWidth={2}
              strokeDasharray="6 3"
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
          return (
            <circle
              key={`ball-${point.ball_no}`}
              data-testid={`point-${point.ball_no}`}
              cx={at.u}
              cy={at.v}
              r={4}
              fill={point.hollow ? "none" : "#d62728"}
              stroke={flagged.has(point.ball_no) ? "#f59e0b" : "#ffffff"}
              strokeWidth={point.hollow || flagged.has(point.ball_no) ? 2 : 0.8}
            >
              <title>{`ball ${point.ball_no} (${point.line} ${point.length}, ${point.source})`}</title>
            </circle>
          );
        })}

      <text x={SVG_WIDTH / 2} y={MARGIN / 2} textAnchor="middle" fontSize={12} fill="#111827">
        {bowlerEndFar ? "bowler end (far)" : "batter end (far)"}
      </text>
      <text
        x={SVG_WIDTH / 2}
        y={SVG_HEIGHT - MARGIN / 4}
        textAnchor="middle"
        fontSize={12}
        fill="#111827"
      >
        {bowlerEndFar ? "batter end (near)" : "bowler end (near)"}
      </text>
      <text
        data-testid="off-side-label"
        x={offRight ? SVG_WIDTH - MARGIN / 4 : MARGIN / 4}
        y={SVG_HEIGHT / 2}
        textAnchor="middle"
        fontSize={11}
        fill="#111827"
        transform={`rotate(${offRight ? 90 : -90} ${offRight ? SVG_WIDTH - MARGIN / 4 : MARGIN / 4} ${SVG_HEIGHT / 2})`}
      >
        off side
      </text>
    </svg>
  );
}
