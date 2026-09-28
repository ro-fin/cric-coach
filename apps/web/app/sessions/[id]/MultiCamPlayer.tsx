"use client";

/**
 * Multi-camera clip player (US-K1, US-B5): HTML5 video, camera switcher with
 * anchor-sync offsets from the clips API, ±1 frame stepping and 0.25×/0.5×
 * slow motion. Keyboard: ←/→ frame-step, number keys switch camera.
 *
 * Frame math runs at the active camera's real fps when the session videos
 * payload exposed it (C7 records 240 fps, docs/camera_setup.md); otherwise
 * the US-B2 lab default is ASSUMED and the readout says so visibly, so the
 * frame number never silently disagrees with vision-side release_frame
 * evidence.
 *
 * Camera switches preserve the absolute session instant: the clips API's
 * start_ms per camera is the anchor offset (see sync.ts).
 */

import { useEffect, useRef, useState } from "react";
import { Button, EmptyState } from "@/components/ui";
import { clipMediaUrl } from "@/lib/api";
import { isEditableTarget } from "./keyboard";
import {
  FRAME_RATE_FPS,
  absoluteMs,
  clipDurationS,
  frameIndex,
  stepFrames,
  switchCameraTime,
} from "./sync";
import type { PlayableClip } from "./timeline";

export const PLAYBACK_RATES: readonly number[] = [0.25, 0.5, 1];

export interface MultiCamPlayerProps {
  ballNo: number;
  cameras: PlayableClip[];
  mediaBase: string;
  /** camera_id -> fps from the session videos payload; null when the viewer's
   * role cannot see it (player) — the default is then assumed, visibly. */
  fpsByCamera?: Record<string, number> | null;
}

export default function MultiCamPlayer({
  ballNo,
  cameras,
  mediaBase,
  fpsByCamera = null,
}: MultiCamPlayerProps) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const timeRef = useRef(0);
  const [activeIdx, setActiveIdx] = useState(0);
  const [rate, setRate] = useState(1);
  const [timeS, setTimeS] = useState(0);

  const active = cameras.length > 0 ? cameras[activeIdx] : null;
  const knownFps =
    active === null || fpsByCamera === null ? null : (fpsByCamera[active.camera_id] ?? null);
  const fps = knownFps ?? FRAME_RATE_FPS;

  function seek(nextS: number) {
    timeRef.current = nextS;
    setTimeS(nextS);
  }

  function step(frames: number) {
    if (active === null) {
      return;
    }
    seek(stepFrames(timeRef.current, frames, clipDurationS(active), fps));
  }

  function switchTo(index: number) {
    if (index === activeIdx) {
      return;
    }
    seek(switchCameraTime(cameras[activeIdx], cameras[index], timeRef.current));
    setActiveIdx(index);
  }

  // Apply state to the media element (anchor-synced time survives src swaps).
  useEffect(() => {
    const video = videoRef.current;
    if (video !== null) {
      video.playbackRate = rate;
      if (video.currentTime !== timeS) {
        video.currentTime = timeS;
      }
    }
  }, [activeIdx, rate, timeS]);

  // Keyboard: ←/→ frame-step, digits 1-8 switch to that camera (US-K1).
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (isEditableTarget(event.target)) {
        return;
      }
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        step(event.key === "ArrowLeft" ? -1 : 1);
      } else if (/^[1-8]$/.test(event.key)) {
        const index = cameras.findIndex((clip) => clip.camera_id === `C${event.key}`);
        if (index !== -1) {
          switchTo(index);
        }
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  });

  if (active === null) {
    return (
      <div data-testid="player-empty">
        <EmptyState
          title={`No playable clips for ball ${ballNo}.`}
          description="Clips appear once the pipeline has cut them from the camera videos."
        />
      </div>
    );
  }

  return (
    <section aria-label={`multi-camera player for ball ${ballNo}`} className="flex flex-col gap-3">
      <video
        data-testid="player-video"
        ref={videoRef}
        controls
        preload="auto"
        className="aspect-video w-full rounded-xl bg-black"
        src={clipMediaUrl(mediaBase, active.object_key)}
        onTimeUpdate={(event) => {
          timeRef.current = event.currentTarget.currentTime;
          setTimeS(event.currentTarget.currentTime);
        }}
      />
      <div role="group" aria-label="cameras" className="flex flex-wrap gap-2">
        {cameras.map((clip, index) => (
          <Button
            key={clip.camera_id}
            variant={index === activeIdx ? "primary" : "secondary"}
            aria-pressed={index === activeIdx}
            onClick={() => switchTo(index)}
          >
            {clip.camera_id}
          </Button>
        ))}
      </div>
      <p data-testid="player-readout" className="font-mono text-sm text-ink-muted">
        {active.camera_id} · t {timeS.toFixed(3)} s · frame {frameIndex(timeS, fps)} · anchor{" "}
        {Math.round(absoluteMs(active, timeS))} ms ·{" "}
        {knownFps === null ? `assumes ${FRAME_RATE_FPS} fps` : `${knownFps} fps`}
      </p>
      <div className="flex flex-wrap gap-4">
        <div role="group" aria-label="frame step" className="flex gap-2">
          <Button variant="secondary" onClick={() => step(-1)}>
            −1 frame
          </Button>
          <Button variant="secondary" onClick={() => step(1)}>
            +1 frame
          </Button>
        </div>
        <div role="group" aria-label="playback rate" className="flex gap-2">
          {PLAYBACK_RATES.map((option) => (
            <Button
              key={option}
              variant={rate === option ? "primary" : "secondary"}
              aria-pressed={rate === option}
              onClick={() => setRate(option)}
            >
              {option}×
            </Button>
          ))}
        </div>
      </div>
    </section>
  );
}
