import { describe, expect, it } from "vitest";
import type { ClipOut, EventOut, TagOut } from "@/lib/api";
import {
  EMPTY_FILTER,
  LENGTH_OPTIONS,
  LINE_OPTIONS,
  OUTCOME_OPTIONS,
  SHOT_OPTIONS,
  applyFilter,
  blockOptions,
  buildBallRows,
  chipSummary,
  chipTone,
  isFilterActive,
  playableClips,
  stepBallNo,
  windowSlice,
} from "./timeline";
import type { BallRow, TimelineFilter } from "./timeline";

function tag(ballNo: number, overrides: Partial<TagOut> = {}): TagOut {
  return {
    ball_no: ballNo,
    block_no: null,
    line: "off",
    length: "good",
    shot: "drive",
    footwork: "front",
    contact: "middle",
    outcome: "controlled_ground_shot",
    control: true,
    source: "manual",
    ground_truth_eligible: true,
    created_by: "parent",
    audits: [],
    ...overrides,
  };
}

function event(ballNo: number, overrides: Partial<EventOut> = {}): EventOut {
  return {
    id: `e${ballNo}`,
    session_id: "s1",
    ball_no: ballNo,
    start_ms: ballNo * 10_000,
    release_ms: ballNo * 10_000 + 500,
    contact_ms: null,
    end_ms: ballNo * 10_000 + 6000,
    confidence: 0.9,
    source: "auto",
    detector_version: "v1",
    valid: true,
    created_at: "2026-07-09T10:00:00Z",
    ...overrides,
  };
}

function clip(ballNo: number, cameraId: string, overrides: Partial<ClipOut> = {}): ClipOut {
  return {
    id: `c${ballNo}-${cameraId}`,
    session_id: "s1",
    ball_no: ballNo,
    camera_id: cameraId,
    object_key: `clips/b${ballNo}_${cameraId}.mp4`,
    start_ms: ballNo * 10_000,
    end_ms: ballNo * 10_000 + 6000,
    status: "cut",
    error: null,
    ...overrides,
  };
}

function row(ballNo: number, overrides: Partial<BallRow> = {}): BallRow {
  return { ballNo, tag: null, event: null, clips: [], ...overrides };
}

describe("buildBallRows", () => {
  it("joins events, tags and clips on ball_no in ascending order", () => {
    const rows = buildBallRows(
      [event(2), event(1)],
      [tag(2), tag(3)],
      [clip(2, "C3"), clip(2, "C1")],
    );
    expect(rows.map((r) => r.ballNo)).toEqual([1, 2, 3]);
    expect(rows[0]).toMatchObject({ tag: null, clips: [] });
    expect(rows[0].event).not.toBeNull();
    expect(rows[1].tag).not.toBeNull();
    expect(rows[1].clips.map((c) => c.camera_id)).toEqual(["C1", "C3"]); // sorted
    expect(rows[2]).toMatchObject({ event: null }); // tag-only ball
  });
});

describe("playableClips", () => {
  it("keeps only CUT clips with an object key", () => {
    const r = row(1, {
      clips: [
        clip(1, "C1"),
        clip(1, "C2", { status: "gap", object_key: null }),
        clip(1, "C3", { status: "cut", object_key: null }),
        clip(1, "C4", { status: "failed" }),
      ],
    });
    expect(playableClips(r).map((c) => c.camera_id)).toEqual(["C1"]);
  });
});

describe("filters (US-K1 AC)", () => {
  const rows = [
    row(1, { tag: tag(1, { block_no: 1, line: "off", control: true }) }),
    row(2, {
      tag: tag(2, {
        block_no: 2,
        line: "leg",
        length: "short",
        shot: "pull",
        outcome: "uncontrolled",
        control: false,
      }),
    }),
    row(3), // untagged
  ];

  it("is inert when empty", () => {
    expect(isFilterActive(EMPTY_FILTER)).toBe(false);
    expect(applyFilter(rows, EMPTY_FILTER)).toBe(rows);
  });

  it.each<[keyof TimelineFilter, TimelineFilter[keyof TimelineFilter], number[]]>([
    ["block", 1, [1]],
    ["line", "leg", [2]],
    ["length", "short", [2]],
    ["shot", "pull", [2]],
    ["outcome", "uncontrolled", [2]],
    ["control", true, [1]],
    ["control", false, [2]],
  ])("filters by %s=%s", (key, value, expected) => {
    const filter = { ...EMPTY_FILTER, [key]: value };
    expect(isFilterActive(filter)).toBe(true);
    expect(applyFilter(rows, filter).map((r) => r.ballNo)).toEqual(expected);
  });

  it("combines criteria and always excludes untagged balls when active", () => {
    const filter: TimelineFilter = {
      ...EMPTY_FILTER,
      line: "leg",
      length: "short",
      shot: "pull",
      outcome: "uncontrolled",
      control: false,
      block: 2,
    };
    expect(applyFilter(rows, filter).map((r) => r.ballNo)).toEqual([2]);
    expect(applyFilter([row(3)], filter)).toEqual([]);
  });

  it("derives sorted distinct block options from tagged rows", () => {
    const withBlocks = [
      row(1, { tag: tag(1, { block_no: 3 }) }),
      row(2, { tag: tag(2, { block_no: 1 }) }),
      row(3, { tag: tag(3, { block_no: 3 }) }),
      row(4, { tag: tag(4, { block_no: null }) }),
      row(5),
    ];
    expect(blockOptions(withBlocks)).toEqual([1, 3]);
  });

  it("pins the full tag vocabularies as filter options", () => {
    expect(LINE_OPTIONS).toHaveLength(4);
    expect(LENGTH_OPTIONS).toHaveLength(4);
    expect(SHOT_OPTIONS).toHaveLength(12);
    expect(OUTCOME_OPTIONS).toHaveLength(7);
  });
});

describe("chipTone (US-K1 AC: color = outcome/contact)", () => {
  it.each([
    ["controlled_ground_shot", "middle", "good"],
    ["controlled_aerial", "middle", "good"],
    ["controlled_ground_shot", "edge", "mixed"], // edged contact downgrades
    ["uncontrolled", "middle", "mixed"],
    ["edged", "edge", "mixed"],
    ["beaten", "miss", "bad"],
    ["bowled", "miss", "bad"],
    ["left_alone", "miss", "neutral"],
  ] as const)("outcome %s + contact %s → %s", (outcome, contact, tone) => {
    expect(chipTone(row(1, { tag: tag(1, { outcome, contact }) }))).toBe(tone);
  });

  it("marks untagged balls", () => {
    expect(chipTone(row(1))).toBe("untagged");
  });
});

describe("chipSummary", () => {
  it("summarizes the tag including the control flag", () => {
    expect(chipSummary(row(1, { tag: tag(1) }))).toBe(
      "off · good · drive · controlled_ground_shot · controlled",
    );
    expect(chipSummary(row(1, { tag: tag(1, { control: false }) }))).toContain("uncontrolled");
  });

  it("labels untagged balls", () => {
    expect(chipSummary(row(1))).toBe("untagged");
  });
});

describe("stepBallNo (↑/↓ ball-step)", () => {
  const rows = [row(1), row(3), row(7)];

  it("keeps the selection when there are no rows", () => {
    expect(stepBallNo([], 5, 1)).toBe(5);
    expect(stepBallNo([], null, -1)).toBeNull();
  });

  it("selects the first row when nothing (or a filtered-out ball) is selected", () => {
    expect(stepBallNo(rows, null, 1)).toBe(1);
    expect(stepBallNo(rows, 4, -1)).toBe(1);
  });

  it("steps and clamps at both ends", () => {
    expect(stepBallNo(rows, 3, 1)).toBe(7);
    expect(stepBallNo(rows, 3, -1)).toBe(1);
    expect(stepBallNo(rows, 1, -1)).toBe(1);
    expect(stepBallNo(rows, 7, 1)).toBe(7);
  });
});

describe("windowSlice (hand-rolled virtualization)", () => {
  it("renders from the top with no top pad", () => {
    const slice = windowSlice(500, 0, 480, 40, 5);
    expect(slice).toEqual({ start: 0, end: 22, padTopPx: 0, padBottomPx: 478 * 40 });
  });

  it("windows the middle with overscan on both sides", () => {
    const slice = windowSlice(500, 4000, 480, 40, 5);
    // floor(4000/40)=100, minus overscan → 95; 12 visible + 10 overscan = 22 rows
    expect(slice).toEqual({ start: 95, end: 117, padTopPx: 95 * 40, padBottomPx: 383 * 40 });
  });

  it("clamps at the bottom of the list", () => {
    const slice = windowSlice(500, 500 * 40, 480, 40, 5);
    expect(slice.end).toBe(500);
    expect(slice.padBottomPx).toBe(0);
  });

  it("renders everything when the list fits the viewport", () => {
    const slice = windowSlice(3, 0, 480, 40, 5);
    expect(slice).toEqual({ start: 0, end: 3, padTopPx: 0, padBottomPx: 0 });
  });

  it("collapses to an empty slice when scrolled past a shrunken list", () => {
    const slice = windowSlice(2, 4000, 480, 40, 5);
    expect(slice.start).toBe(slice.end);
    expect(slice.padBottomPx).toBe(0);
  });
});
