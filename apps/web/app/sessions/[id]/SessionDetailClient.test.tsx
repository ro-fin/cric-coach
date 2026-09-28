import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, ClipOut, EventOut, SessionOut, TagOut, VideoOut } from "@/lib/api";
import SessionDetailClient from "./SessionDetailClient";

const SESSION: SessionOut = {
  id: "s1",
  player_id: "p1",
  session_date: "2026-07-09",
  session_type: "batting",
  bowler_source: "machine",
  machine_settings: { speed_kph: 100, length: "good" },
  notes: null,
  state: "analyzed",
  degraded: false,
  missing_views: [],
};

function tag(ballNo: number, overrides: Partial<TagOut> = {}): TagOut {
  return {
    ball_no: ballNo,
    block_no: 1,
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

function event(ballNo: number): EventOut {
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
  };
}

function clip(ballNo: number, cameraId: string, startMs: number): ClipOut {
  return {
    id: `c${ballNo}-${cameraId}`,
    session_id: "s1",
    ball_no: ballNo,
    camera_id: cameraId,
    object_key: `clips/b${ballNo}_${cameraId}.mp4`,
    start_ms: startMs,
    end_ms: startMs + 6000,
    status: "cut",
    error: null,
  };
}

function videoRow(cameraId: string, claimedFps: number | null): VideoOut {
  return {
    id: `v-${cameraId}`,
    session_id: "s1",
    camera_id: cameraId,
    object_key: `videos/s1/${cameraId}.mp4`,
    filename: `${cameraId}.mp4`,
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

const EVENTS = [event(1), event(2), event(3)];
const TAGS = [
  tag(1),
  tag(2, { line: "leg", length: "short", shot: "pull", outcome: "uncontrolled", control: false }),
];
const CLIPS = [clip(1, "C1", 10_000), clip(1, "C2", 10_500)];

interface Overrides {
  session?: SessionOut;
  getSession?: ApiClient["getSession"];
  listEvents?: ApiClient["listEvents"];
  listTags?: ApiClient["listTags"];
  listClips?: ApiClient["listClips"];
  listSessionVideos?: ApiClient["listSessionVideos"];
}

function fakeClient(overrides: Overrides = {}): ApiClient {
  return {
    listSessions: vi.fn(),
    getSession: vi.fn(overrides.getSession ?? (async () => overrides.session ?? SESSION)),
    listEvents: vi.fn(overrides.listEvents ?? (async () => EVENTS)),
    listTags: vi.fn(overrides.listTags ?? (async () => TAGS)),
    listClips: vi.fn(overrides.listClips ?? (async () => CLIPS)),
    listSessionVideos: vi.fn(overrides.listSessionVideos ?? (async () => [])),
    ballMetrics: vi.fn(async () => []),
  };
}

function renderDetail(client: ApiClient = fakeClient()) {
  return render(
    <SessionDetailClient sessionId="s1" client={client} mediaBase="http://nas:9000/cricai" />,
  );
}

async function detailLoaded(): Promise<void> {
  await screen.findByRole("heading", { name: /Session 2026-07-09/ });
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

describe("SessionDetailClient (US-K1, US-B5)", () => {
  it("loads, then opens on the first ball with synchronized-start playback", async () => {
    renderDetail();
    expect(screen.getByTestId("detail-loading")).toBeInTheDocument();
    await detailLoaded();
    expect(
      screen.getByRole("heading", { name: "Session 2026-07-09 · batting" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/analyzed · machine · 3 balls/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Ball 1" })).toBeInTheDocument();
    // US-B5: playback opens at the ball's anchor start on every camera.
    expect(screen.getByTestId("player-readout")).toHaveTextContent(
      "C1 · t 0.000 s · frame 0 · anchor 10000 ms",
    );
    expect(screen.getByTestId("player-video")).toHaveAttribute(
      "src",
      "http://nas:9000/cricai/clips/b1_C1.mp4",
    );
  });

  it("flags degraded captures in the header", async () => {
    renderDetail(fakeClient({ session: { ...SESSION, degraded: true } }));
    await detailLoaded();
    expect(screen.getByText(/degraded capture/)).toBeInTheDocument();
  });

  it("clicking a timeline chip opens that ball in the player (US-K1 AC)", async () => {
    const user = userEvent.setup();
    renderDetail();
    await detailLoaded();
    await user.click(screen.getByText("Ball 2").closest("button") as HTMLButtonElement);
    expect(screen.getByRole("heading", { name: "Ball 2" })).toBeInTheDocument();
    // ball 2 has no CUT clips: the gap is loud, never silent (US-D2).
    expect(screen.getByTestId("player-empty")).toBeInTheDocument();
  });

  it("steps balls with ↑/↓ across the timeline, clamped at the ends", async () => {
    renderDetail();
    await detailLoaded();
    fireEvent.keyDown(document.body, { key: "ArrowDown" });
    expect(screen.getByRole("heading", { name: "Ball 2" })).toBeInTheDocument();
    fireEvent.keyDown(document.body, { key: "ArrowUp" });
    fireEvent.keyDown(document.body, { key: "ArrowUp" }); // clamped at ball 1
    expect(screen.getByRole("heading", { name: "Ball 1" })).toBeInTheDocument();
    fireEvent.keyDown(document.body, { key: "Enter" }); // unbound key
    expect(screen.getByRole("heading", { name: "Ball 1" })).toBeInTheDocument();
  });

  it("ignores ball-step keys typed into form controls", async () => {
    renderDetail();
    await detailLoaded();
    fireEvent.keyDown(screen.getByLabelText("Line"), { key: "ArrowDown" });
    expect(screen.getByRole("heading", { name: "Ball 1" })).toBeInTheDocument();
  });

  it("filters the timeline and ball-steps within the filtered set", async () => {
    const user = userEvent.setup();
    renderDetail();
    await detailLoaded();
    await user.selectOptions(screen.getByLabelText("Line"), "leg");
    const timeline = within(screen.getByRole("list", { name: "ball timeline" }));
    expect(timeline.queryByText("Ball 1")).not.toBeInTheDocument();
    expect(timeline.getByText("Ball 2")).toBeInTheDocument();
    // selection (ball 1) is no longer in the filtered set: ↓ lands on ball 2.
    fireEvent.keyDown(document.body, { key: "ArrowDown" });
    expect(screen.getByRole("heading", { name: "Ball 2" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(
      within(screen.getByRole("list", { name: "ball timeline" })).getByText("Ball 1"),
    ).toBeInTheDocument();
  });

  it("says so when the session has no balls yet", async () => {
    renderDetail(
      fakeClient({ listEvents: async () => [], listTags: async () => [] }),
    );
    await detailLoaded();
    expect(screen.getByTestId("no-selection")).toBeInTheDocument();
    fireEvent.keyDown(document.body, { key: "ArrowDown" }); // no rows: no-op
    expect(screen.getByTestId("no-selection")).toBeInTheDocument();
  });

  it("surfaces load failures, Error or not", async () => {
    renderDetail(
      fakeClient({
        getSession: async () => {
          throw new Error("API 404: session not found");
        },
      }),
    );
    expect(await screen.findByTestId("detail-error")).toHaveTextContent(
      "Could not load session: API 404: session not found",
    );
  });

  it("stringifies non-Error load failures", async () => {
    renderDetail(fakeClient({ listClips: () => Promise.reject("wire down") }));
    expect(await screen.findByTestId("detail-error")).toHaveTextContent("wire down");
  });

  it("ignores results and errors that land after unmount", async () => {
    let resolve!: (session: SessionOut) => void;
    const first = renderDetail(
      fakeClient({ getSession: () => new Promise((r) => (resolve = r)) }),
    );
    first.unmount();
    await act(async () => {
      resolve(SESSION);
    });

    let reject!: (reason: unknown) => void;
    const second = renderDetail(
      fakeClient({ getSession: () => new Promise((_, r) => (reject = r)) }),
    );
    second.unmount();
    await act(async () => {
      reject(new Error("late"));
    });
    expect(screen.queryByTestId("detail-error")).not.toBeInTheDocument();
  });

  it("opens on the ?ball=N deep link from the pitch map (US-K2→US-K1 URL contract)", async () => {
    window.history.replaceState(null, "", "/sessions/s1?ball=2");
    renderDetail();
    await detailLoaded();
    expect(screen.getByRole("heading", { name: "Ball 2" })).toBeInTheDocument();
    const timeline = within(screen.getByRole("list", { name: "ball timeline" }));
    const chip = timeline.getByText("Ball 2").closest("button") as HTMLButtonElement;
    expect(chip).toHaveAttribute("aria-pressed", "true");
  });

  it.each([
    ["an unknown ball number", "?ball=999"],
    ["a malformed ball value", "?ball=abc"],
    ["a non-positive ball value", "?ball=0"],
  ])("falls back to the first ball for %s", async (_label, search) => {
    window.history.replaceState(null, "", `/sessions/s1${search}`);
    renderDetail();
    await detailLoaded();
    expect(screen.getByRole("heading", { name: "Ball 1" })).toBeInTheDocument();
  });

  it("derives frame math from the session videos' per-camera fps when visible (US-I2 seam)", async () => {
    const user = userEvent.setup();
    renderDetail(
      fakeClient({
        listSessionVideos: async () => [videoRow("C1", 240), videoRow("C2", null)],
      }),
    );
    await detailLoaded();
    // C1 carries claimed_fps 240: the readout names the real rate, no guess.
    expect(await screen.findByTestId("player-readout")).toHaveTextContent("240 fps");
    expect(screen.getByTestId("player-readout")).not.toHaveTextContent("assumes");
    // ±1 frame at 240 fps is 1/240 s, not 1/120 s.
    await user.click(screen.getByRole("button", { name: "+1 frame" }));
    expect(screen.getByTestId("player-readout")).toHaveTextContent("t 0.004 s · frame 1");
    // C2 has no usable fps: the default is assumed, visibly.
    await user.click(screen.getByRole("button", { name: "C2" }));
    expect(screen.getByTestId("player-readout")).toHaveTextContent("assumes 120 fps");
  });

  it("assumes 120 fps visibly when the videos endpoint is not readable (player role)", async () => {
    renderDetail(
      fakeClient({
        listSessionVideos: async () => {
          throw new Error("API 401: missing bearer token");
        },
      }),
    );
    await detailLoaded();
    expect(await screen.findByTestId("player-readout")).toHaveTextContent("assumes 120 fps");
  });

  it("builds a default client and media base from the environment", async () => {
    const fetchFn = vi.fn(async (url: string) => {
      const body = url.endsWith("/sessions/s1") ? JSON.stringify(SESSION) : "[]";
      return new Response(body, {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchFn);
    render(<SessionDetailClient sessionId="s1" />);
    await detailLoaded();
    expect(screen.getByTestId("no-selection")).toBeInTheDocument();
    expect(fetchFn).toHaveBeenCalledWith(
      expect.stringMatching(/\/sessions\/s1$/),
      expect.anything(),
    );
  });
});
