import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import MultiCamPlayer from "./MultiCamPlayer";
import { FRAME_STEP_S, absoluteMs } from "./sync";
import type { PlayableClip } from "./timeline";

function cam(cameraId: string, startMs: number, endMs: number): PlayableClip {
  return {
    id: `clip-${cameraId}`,
    session_id: "s1",
    ball_no: 1,
    camera_id: cameraId,
    object_key: `clips/b1_${cameraId}.mp4`,
    start_ms: startMs,
    end_ms: endMs,
    status: "cut",
    error: null,
  };
}

const C1 = cam("C1", 1000, 3000);
const C2 = cam("C2", 1500, 3500);
const MEDIA = "http://nas:9000/cricai";

function video(): HTMLVideoElement {
  return screen.getByTestId("player-video") as HTMLVideoElement;
}

function readout(): HTMLElement {
  return screen.getByTestId("player-readout");
}

function renderPlayer(cameras: PlayableClip[] = [C1, C2]) {
  return render(<MultiCamPlayer ballNo={1} cameras={cameras} mediaBase={MEDIA} />);
}

describe("MultiCamPlayer (US-K1/US-B5)", () => {
  it("plays the first camera from the clip start with its media URL", () => {
    renderPlayer();
    expect(video()).toHaveAttribute("src", `${MEDIA}/clips/b1_C1.mp4`);
    expect(readout()).toHaveTextContent("C1 · t 0.000 s · frame 0 · anchor 1000 ms");
    expect(screen.getByRole("button", { name: "C1" })).toHaveAttribute("aria-pressed", "true");
  });

  it("steps ±1 frame at 120 fps and applies it to the video element", async () => {
    const user = userEvent.setup();
    renderPlayer();
    await user.click(screen.getByRole("button", { name: "+1 frame" }));
    expect(video().currentTime).toBeCloseTo(FRAME_STEP_S, 10);
    expect(readout()).toHaveTextContent("t 0.008 s · frame 1");
    await user.click(screen.getByRole("button", { name: "−1 frame" }));
    expect(video().currentTime).toBe(0);
    // clamped at the clip start
    await user.click(screen.getByRole("button", { name: "−1 frame" }));
    expect(readout()).toHaveTextContent("frame 0");
  });

  it("switches cameras preserving the absolute instant within ±2 frames (US-K1 AC)", async () => {
    const user = userEvent.setup();
    renderPlayer();
    video().currentTime = 1.0; // scrub to abs 2000 ms on C1
    fireEvent.timeUpdate(video());
    expect(readout()).toHaveTextContent("anchor 2000 ms");
    await user.click(screen.getByRole("button", { name: "C2" }));
    expect(video()).toHaveAttribute("src", `${MEDIA}/clips/b1_C2.mp4`);
    expect(screen.getByRole("button", { name: "C2" })).toHaveAttribute("aria-pressed", "true");
    expect(readout()).toHaveTextContent("C2 · t 0.500 s");
    const drift = Math.abs(absoluteMs(C2, 0.5) - absoluteMs(C1, 1.0));
    expect(drift).toBeLessThanOrEqual(2 * FRAME_STEP_S * 1000);
    expect(video().currentTime).toBeCloseTo(0.5, 10);
  });

  it("ignores a click on the already-active camera", async () => {
    const user = userEvent.setup();
    renderPlayer();
    await user.click(screen.getByRole("button", { name: "C1" }));
    expect(readout()).toHaveTextContent("C1 · t 0.000 s");
  });

  it("offers 0.25×/0.5× slow motion (US-B5 AC)", async () => {
    const user = userEvent.setup();
    renderPlayer();
    await user.click(screen.getByRole("button", { name: "0.25×" }));
    expect(video().playbackRate).toBe(0.25);
    expect(screen.getByRole("button", { name: "0.25×" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await user.click(screen.getByRole("button", { name: "0.5×" }));
    expect(video().playbackRate).toBe(0.5);
  });

  it("frame-steps with ←/→ and switches cameras with number keys (US-K1 AC)", () => {
    renderPlayer();
    fireEvent.keyDown(document.body, { key: "ArrowRight" });
    expect(readout()).toHaveTextContent("frame 1");
    fireEvent.keyDown(document.body, { key: "ArrowLeft" });
    expect(readout()).toHaveTextContent("frame 0");
    fireEvent.keyDown(document.body, { key: "2" });
    expect(readout()).toHaveTextContent(/^C2/);
    fireEvent.keyDown(document.body, { key: "2" }); // already active: no-op
    expect(readout()).toHaveTextContent(/^C2/);
    fireEvent.keyDown(document.body, { key: "7" }); // no C7 in this ball
    expect(readout()).toHaveTextContent(/^C2/);
    fireEvent.keyDown(document.body, { key: "x" }); // unbound key
    expect(readout()).toHaveTextContent(/^C2/);
  });

  it("leaves keystrokes in form controls alone", () => {
    renderPlayer();
    const select = document.createElement("select");
    document.body.appendChild(select);
    fireEvent.keyDown(select, { key: "ArrowRight" });
    expect(readout()).toHaveTextContent("frame 0");
    select.remove();
  });

  it("reports a ball with no playable clips and survives keyboard input", () => {
    renderPlayer([]);
    expect(screen.getByTestId("player-empty")).toHaveTextContent(
      "No playable clips for ball 1.",
    );
    fireEvent.keyDown(document.body, { key: "ArrowRight" }); // step() guard
    fireEvent.keyDown(document.body, { key: "1" }); // no cameras to switch to
    expect(screen.getByTestId("player-empty")).toBeInTheDocument();
  });

  it("resyncs its readout from native scrubbing via timeupdate", () => {
    renderPlayer();
    video().currentTime = 0.25;
    fireEvent.timeUpdate(video());
    expect(readout()).toHaveTextContent("t 0.250 s · frame 30 · anchor 1250 ms");
  });

  it("says visibly that 120 fps is assumed when no per-camera fps is known", () => {
    renderPlayer();
    expect(readout()).toHaveTextContent("assumes 120 fps");
  });

  it("uses the camera's real fps for frame readout and stepping when known", async () => {
    // C7-style 240 fps camera (docs/camera_setup.md): frame numbers must match
    // the vision side's release_frame indexing, and ±1 frame is 1/240 s.
    const user = userEvent.setup();
    render(
      <MultiCamPlayer
        ballNo={1}
        cameras={[C1, C2]}
        mediaBase={MEDIA}
        fpsByCamera={{ C1: 240 }}
      />,
    );
    expect(readout()).toHaveTextContent("240 fps");
    expect(readout()).not.toHaveTextContent("assumes");
    await user.click(screen.getByRole("button", { name: "+1 frame" }));
    expect(readout()).toHaveTextContent("t 0.004 s · frame 1");
    video().currentTime = 0.5;
    fireEvent.timeUpdate(video());
    expect(readout()).toHaveTextContent("frame 120");
    // C2 is absent from the fps map: the assumption becomes visible again.
    await user.click(screen.getByRole("button", { name: "C2" }));
    expect(readout()).toHaveTextContent("assumes 120 fps");
  });
});
