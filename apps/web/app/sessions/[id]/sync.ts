/**
 * Multi-camera anchor-sync and frame-step math (US-K1, US-B5).
 *
 * Each clip row from the clips API carries the camera's cut window
 * (start_ms/end_ms) on the session's shared timebase — the anchor offsets.
 * Camera switches preserve the absolute session instant by converting the
 * active camera's local playback time through those offsets.
 */

import type { ClipOut, VideoOut } from "@/lib/api";

/** Lab capture standard (US-B2 gates 120 fps) — the ASSUMED rate when the
 * per-camera fps is not visible to the viewer (clip rows carry no fps and
 * /sessions/{id}/videos is parent/coach-only). C7 records at 240 fps
 * (docs/camera_setup.md), so an assumption must always be shown as one. */
export const FRAME_RATE_FPS = 120;

/** One frame at the assumed lab capture rate, in seconds. */
export const FRAME_STEP_S = 1 / FRAME_RATE_FPS;

/** Per-camera fps from the session videos payload (US-B2 claimed_fps).
 * First row per camera wins (rows arrive ordered by camera_id, filename);
 * cameras without a usable claim are simply absent — the player then shows
 * the assumed-rate caption instead of a silently wrong frame number. */
export function fpsByCamera(videos: VideoOut[]): Record<string, number> {
  const map: Record<string, number> = {};
  for (const video of videos) {
    if (video.claimed_fps !== null && !(video.camera_id in map)) {
      map[video.camera_id] = video.claimed_fps;
    }
  }
  return map;
}

/** Clip length in seconds from its cut window. */
export function clipDurationS(clip: ClipOut): number {
  return (clip.end_ms - clip.start_ms) / 1000;
}

/** Clamp a local playback time into [0, duration]. */
export function clampTime(timeS: number, durationS: number): number {
  return Math.min(Math.max(timeS, 0), Math.max(durationS, 0));
}

/** Absolute session instant (ms) for a local playback time on one camera. */
export function absoluteMs(clip: ClipOut, localS: number): number {
  return clip.start_ms + localS * 1000;
}

/** Local playback time on a camera for an absolute session instant, clamped. */
export function localSeconds(clip: ClipOut, absMs: number): number {
  return clampTime((absMs - clip.start_ms) / 1000, clipDurationS(clip));
}

/** Where the target camera must seek so both show the same instant (±2 frames). */
export function switchCameraTime(from: ClipOut, to: ClipOut, localS: number): number {
  return localSeconds(to, absoluteMs(from, localS));
}

/** Step by whole frames at the camera's capture rate, clamped to the clip
 * window (US-K1 ±1-frame stepping; fps is per-camera, C7 runs 240). */
export function stepFrames(
  localS: number,
  frames: number,
  durationS: number,
  fps: number,
): number {
  return clampTime(localS + frames * (1 / fps), durationS);
}

/** Frame index at the camera's capture rate for a local playback time —
 * matches the vision side's release_frame indexing (track.fps). */
export function frameIndex(localS: number, fps: number): number {
  return Math.round(localS * fps);
}
