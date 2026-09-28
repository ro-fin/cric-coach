import { describe, expect, it } from "vitest";
import type { ClipOut, VideoOut } from "@/lib/api";
import {
  FRAME_RATE_FPS,
  FRAME_STEP_S,
  absoluteMs,
  clampTime,
  clipDurationS,
  fpsByCamera,
  frameIndex,
  localSeconds,
  stepFrames,
  switchCameraTime,
} from "./sync";

function clip(cameraId: string, startMs: number, endMs: number): ClipOut {
  return {
    id: `clip-${cameraId}`,
    session_id: "s1",
    ball_no: 1,
    camera_id: cameraId,
    object_key: `clips/${cameraId}.mp4`,
    start_ms: startMs,
    end_ms: endMs,
    status: "cut",
    error: null,
  };
}

describe("clipDurationS", () => {
  it("derives seconds from the cut window", () => {
    expect(clipDurationS(clip("C1", 1000, 3500))).toBe(2.5);
  });
});

describe("clampTime", () => {
  it("clamps below zero, above duration and passes in-range values", () => {
    expect(clampTime(-0.5, 2)).toBe(0);
    expect(clampTime(3, 2)).toBe(2);
    expect(clampTime(1.25, 2)).toBe(1.25);
  });

  it("treats a negative duration as an empty window", () => {
    expect(clampTime(1, -2)).toBe(0);
  });
});

describe("anchor sync (US-K1: camera switch preserves the session instant)", () => {
  const c1 = clip("C1", 1000, 3000);
  const c2 = clip("C2", 1500, 3500);

  it("converts local playback time to the absolute session instant", () => {
    expect(absoluteMs(c1, 0.5)).toBe(1500);
  });

  it("converts an absolute instant to camera-local time", () => {
    expect(localSeconds(c2, 2000)).toBe(0.5);
  });

  it("clamps instants outside the target camera's window", () => {
    expect(localSeconds(c2, 1000)).toBe(0);
    expect(localSeconds(c2, 9000)).toBe(2);
  });

  it("preserves the absolute instant across a switch within ±2 frames", () => {
    const local = switchCameraTime(c1, c2, 1.0); // abs 2000ms
    const drift = Math.abs(absoluteMs(c2, local) - absoluteMs(c1, 1.0));
    expect(drift).toBeLessThanOrEqual(2 * FRAME_STEP_S * 1000);
    expect(local).toBe(0.5);
  });
});

function videoRow(cameraId: string, claimedFps: number | null, filename: string): VideoOut {
  return {
    id: `v-${cameraId}-${filename}`,
    session_id: "s1",
    camera_id: cameraId,
    object_key: `videos/s1/${filename}`,
    filename,
    checksum_sha256: "c".repeat(64),
    size_bytes: 1024,
    claimed_fps: claimedFps,
    claimed_resolution: null,
    claimed_duration_s: null,
    codec: null,
    status: "probed",
    probe: null,
    error: null,
  };
}

describe("fpsByCamera", () => {
  it("maps each camera to its first usable claimed fps, skipping null claims", () => {
    expect(
      fpsByCamera([
        videoRow("C1", 120, "a.mp4"),
        videoRow("C1", 60, "b.mp4"), // later row for the same camera: first wins
        videoRow("C7", 240, "c.mp4"),
        videoRow("C2", null, "d.mp4"), // no usable claim: honestly absent
      ]),
    ).toEqual({ C1: 120, C7: 240 });
  });
});

describe("stepFrames", () => {
  it("steps by whole frames at the camera's capture rate", () => {
    expect(FRAME_RATE_FPS).toBe(120);
    expect(stepFrames(0, 1, 2, FRAME_RATE_FPS)).toBeCloseTo(1 / 120, 10);
    expect(stepFrames(1, -1, 2, FRAME_RATE_FPS)).toBeCloseTo(1 - 1 / 120, 10);
    // a 240 fps camera (C7 per docs/camera_setup.md) steps half as far
    expect(stepFrames(0, 1, 2, 240)).toBeCloseTo(1 / 240, 10);
  });

  it("clamps at the clip boundaries", () => {
    expect(stepFrames(0, -1, 2, FRAME_RATE_FPS)).toBe(0);
    expect(stepFrames(2, 1, 2, FRAME_RATE_FPS)).toBe(2);
  });
});

describe("frameIndex", () => {
  it("rounds local time to the nearest frame at the camera's rate", () => {
    expect(frameIndex(0, FRAME_RATE_FPS)).toBe(0);
    expect(frameIndex(1, FRAME_RATE_FPS)).toBe(120);
    expect(frameIndex(0.0125, FRAME_RATE_FPS)).toBe(2); // 1.5 frames rounds to 2
    // vision-side release_frame indexes at the true rate: t=0.5s on 240 fps is frame 120
    expect(frameIndex(0.5, 240)).toBe(120);
  });
});
