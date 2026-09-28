/**
 * Ball-by-ball timeline assembly, filtering and windowed-rendering math
 * (US-K1, US-B5). Pure functions over the wire shapes from lib/api — the
 * timeline joins server rows verbatim and never computes metrics client-side.
 */

import type { ClipOut, EventOut, LengthZone, Line, Outcome, Shot, TagOut } from "@/lib/api";

/** One timeline row: the union of what the API knows about a ball number. */
export interface BallRow {
  ballNo: number;
  tag: TagOut | null;
  event: EventOut | null;
  /** All clip rows for the ball, every status, ordered by camera_id. */
  clips: ClipOut[];
}

/** Join events (valid timeline), tags and clips on ball_no, ascending. */
export function buildBallRows(events: EventOut[], tags: TagOut[], clips: ClipOut[]): BallRow[] {
  const tagByBall = new Map(tags.map((tag) => [tag.ball_no, tag]));
  const eventByBall = new Map(events.map((event) => [event.ball_no, event]));
  const clipsByBall = new Map<number, ClipOut[]>();
  for (const clip of clips) {
    const forBall = clipsByBall.get(clip.ball_no) ?? [];
    forBall.push(clip);
    clipsByBall.set(clip.ball_no, forBall);
  }
  const ballNos = [...new Set([...eventByBall.keys(), ...tagByBall.keys()])].sort(
    (a, b) => a - b,
  );
  return ballNos.map((ballNo) => ({
    ballNo,
    tag: tagByBall.get(ballNo) ?? null,
    event: eventByBall.get(ballNo) ?? null,
    clips: (clipsByBall.get(ballNo) ?? []).sort((a, b) =>
      a.camera_id.localeCompare(b.camera_id),
    ),
  }));
}

/** A clip the player can load: status CUT with a stored object key. */
export interface PlayableClip extends ClipOut {
  object_key: string;
}

/** Playable camera set for a ball: CUT clips with an object key (US-D2). */
export function playableClips(row: BallRow): PlayableClip[] {
  return row.clips.filter(
    (clip): clip is PlayableClip => clip.status === "cut" && clip.object_key !== null,
  );
}

/** Step the selection through rows (↑/↓ ball-step, US-K1), clamped at ends. */
export function stepBallNo(
  rows: BallRow[],
  selectedBallNo: number | null,
  delta: number,
): number | null {
  if (rows.length === 0) {
    return selectedBallNo;
  }
  const index = rows.findIndex((row) => row.ballNo === selectedBallNo);
  if (index === -1) {
    return rows[0].ballNo;
  }
  const next = Math.min(Math.max(index + delta, 0), rows.length - 1);
  return rows[next].ballNo;
}

// ---------------------------------------------------------------------------
// Filters (US-K1: block, line, length, shot, outcome, control).
// ---------------------------------------------------------------------------

export interface TimelineFilter {
  block: number | null;
  line: Line | null;
  length: LengthZone | null;
  shot: Shot | null;
  outcome: Outcome | null;
  control: boolean | null;
}

export const EMPTY_FILTER: TimelineFilter = {
  block: null,
  line: null,
  length: null,
  shot: null,
  outcome: null,
  control: null,
};

export function isFilterActive(filter: TimelineFilter): boolean {
  return Object.values(filter).some((value) => value !== null);
}

function tagMatches(tag: TagOut, filter: TimelineFilter): boolean {
  return (
    (filter.block === null || tag.block_no === filter.block) &&
    (filter.line === null || tag.line === filter.line) &&
    (filter.length === null || tag.length === filter.length) &&
    (filter.shot === null || tag.shot === filter.shot) &&
    (filter.outcome === null || tag.outcome === filter.outcome) &&
    (filter.control === null || tag.control === filter.control)
  );
}

/** Filter rows; criteria live on tags, so an active filter excludes untagged balls. */
export function applyFilter(rows: BallRow[], filter: TimelineFilter): BallRow[] {
  if (!isFilterActive(filter)) {
    return rows;
  }
  return rows.filter((row) => row.tag !== null && tagMatches(row.tag, filter));
}

/** Distinct block numbers present in the rows' tags, ascending. */
export function blockOptions(rows: BallRow[]): number[] {
  const blocks = new Set<number>();
  for (const row of rows) {
    if (row.tag !== null && row.tag.block_no !== null) {
      blocks.add(row.tag.block_no);
    }
  }
  return [...blocks].sort((a, b) => a - b);
}

export const LINE_OPTIONS: readonly Line[] = ["outside_off", "off", "middle", "leg"];
export const LENGTH_OPTIONS: readonly LengthZone[] = ["yorker", "full", "good", "short"];
export const SHOT_OPTIONS: readonly Shot[] = [
  "leave",
  "defend",
  "drive",
  "cover_drive",
  "straight_drive",
  "on_drive",
  "cut",
  "pull",
  "hook",
  "sweep",
  "flick",
  "loft",
];
export const OUTCOME_OPTIONS: readonly Outcome[] = [
  "controlled_ground_shot",
  "controlled_aerial",
  "uncontrolled",
  "beaten",
  "bowled",
  "edged",
  "left_alone",
];

// ---------------------------------------------------------------------------
// Chip presentation (US-K1 AC: chip color = outcome/contact).
// ---------------------------------------------------------------------------

export type ChipTone = "good" | "mixed" | "bad" | "neutral" | "untagged";

const OUTCOME_TONE: Record<Outcome, ChipTone> = {
  controlled_ground_shot: "good",
  controlled_aerial: "good",
  uncontrolled: "mixed",
  edged: "mixed",
  beaten: "bad",
  bowled: "bad",
  left_alone: "neutral",
};

/** Chip color class from the tag's outcome, downgraded on an edged contact. */
export function chipTone(row: BallRow): ChipTone {
  if (row.tag === null) {
    return "untagged";
  }
  const tone = OUTCOME_TONE[row.tag.outcome];
  return tone === "good" && row.tag.contact === "edge" ? "mixed" : tone;
}

/** One-line tag summary for a chip; untagged balls say so explicitly. */
export function chipSummary(row: BallRow): string {
  if (row.tag === null) {
    return "untagged";
  }
  const control = row.tag.control ? "controlled" : "uncontrolled";
  return `${row.tag.line} · ${row.tag.length} · ${row.tag.shot} · ${row.tag.outcome} · ${control}`;
}

// ---------------------------------------------------------------------------
// Hand-rolled windowed rendering (US-K1 PT: 500-ball index < 2 s).
// ---------------------------------------------------------------------------

export interface WindowSlice {
  /** Index of the first rendered row. */
  start: number;
  /** Exclusive end index of the rendered rows. */
  end: number;
  /** Spacer height (px) standing in for the rows above the window. */
  padTopPx: number;
  /** Spacer height (px) standing in for the rows below the window. */
  padBottomPx: number;
}

/** Compute the visible window: only rows near the viewport render to DOM. */
export function windowSlice(
  total: number,
  scrollTopPx: number,
  viewportPx: number,
  rowPx: number,
  overscan: number,
): WindowSlice {
  const first = Math.max(0, Math.floor(scrollTopPx / rowPx) - overscan);
  const visible = Math.ceil(viewportPx / rowPx) + 2 * overscan;
  const end = Math.min(total, first + visible);
  return {
    start: Math.min(first, end),
    end,
    padTopPx: Math.min(first, end) * rowPx,
    padBottomPx: (total - end) * rowPx,
  };
}
