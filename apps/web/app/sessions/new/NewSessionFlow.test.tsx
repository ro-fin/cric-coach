import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import type { SessionOut } from "@/lib/api";
import { expectNoA11yViolations } from "@/test/axe";
import { player } from "@/test/fixtures.core";
import { session } from "@/test/fixtures";
import type { CameraOut, LifecycleOut, NewSessionApi } from "./api";
import NewSessionFlow from "./NewSessionFlow";

function camera(cameraId: string, o: Partial<CameraOut> = {}): CameraOut {
  return {
    id: `cfg-${cameraId}`,
    camera_id: cameraId,
    era_no: 1,
    active: true,
    role: null,
    position_label: `${cameraId} side-on`,
    xyz_offset_m: { x: 0, y: 0, z: 0 },
    height_m: 1.2,
    fps: 120,
    resolution: "1920x1080",
    lens: "",
    mount: "",
    protected: true,
    fov_reference_key: null,
    reserved_for_future: false,
    ...o,
  };
}

function lifecycle(o: Partial<LifecycleOut> = {}): LifecycleOut {
  return {
    state: "created",
    degraded: false,
    missing_views: [],
    started_at: null,
    stopped_at: null,
    ...o,
  };
}

const CREATED = session({ id: "s9", state: "created", bowler_source: "machine" });

function fakeApi(o: Partial<NewSessionApi> = {}): NewSessionApi {
  return {
    listPlayers: vi.fn(async () => [player(), player({ id: "p2", name: "Meera", is_guest: true })]),
    createSession: vi.fn(async (payload) => ({
      ...CREATED,
      bowler_source: payload.bowler_source,
    })),
    getSession: vi.fn(async () => CREATED),
    machineChecklist: vi.fn(async () => ({
      items: [
        { id: "helmet_on", label: "Batter wearing a helmet for machine batting" },
        { id: "estop_tested", label: "Emergency stop tested and within reach" },
      ],
    })),
    ackChecklist: vi.fn(async () => ({
      id: "ack1",
      items: { helmet_on: true, estop_tested: true },
      acked_by: "Mum",
      acked_at: "2026-09-28T10:00:00Z",
    })),
    listCameras: vi.fn(async () => [camera("C1"), camera("C2"), camera("C3", { active: false })]),
    start: vi.fn(async (_id, cameras) => ({
      state: "recording" as const,
      started_at: "2026-09-28T10:01:00Z",
      cameras,
      warnings: ["C1 is registered at 60 fps, below the 120 fps floor"],
      idempotent: false,
    })),
    stop: vi.fn(async (_id, payload) => ({
      state: "captured" as const,
      degraded: Object.keys(payload.cameras_reporting).length > 0,
      missing_views: Object.keys(payload.cameras_reporting),
      stopped_at: "2026-09-28T10:30:00Z",
      idempotent: false,
    })),
    lifecycle: vi.fn(async () => lifecycle()),
    ...o,
  };
}

const NOW = () => new Date(2026, 8, 28, 9, 0);

function renderFlow(api: NewSessionApi, search = "", role: "parent" | "coach" | "player" = "parent") {
  return render(<NewSessionFlow role={role} search={search} api={api} now={NOW} />);
}

afterEach(() => {
  window.history.replaceState(null, "", "/");
});

async function fillDetails(user: ReturnType<typeof userEvent.setup>, speed = "95") {
  await screen.findByLabelText("Player");
  await user.type(screen.getByLabelText("Speed (km/h)"), speed);
}

describe("NewSessionFlow", () => {
  it("runs a machine session end to end with every server verdict verbatim", async () => {
    const api = fakeApi();
    const user = userEvent.setup();
    renderFlow(api, "?player=p2");
    const steps = await screen.findByRole("list", { name: "Steps" });
    expect(within(steps).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "1. Details",
      "2. Safety check",
      "3. Cameras",
      "4. Recording",
      "5. Done",
    ]);
    expect(await screen.findByLabelText("Player")).toHaveValue("p2");
    expect(screen.getByRole("option", { name: "Meera (guest)" })).toBeInTheDocument();
    expect(screen.getByLabelText("Date")).toHaveValue("2026-09-28");

    // client-side check mirrors the server's speed range
    await user.click(screen.getByRole("button", { name: "Create session" }));
    expect(screen.getByText(/machine speed from 30 to 160/)).toBeInTheDocument();
    expect(api.createSession).not.toHaveBeenCalled();

    await user.type(screen.getByLabelText("Speed (km/h)"), "95");
    await user.selectOptions(screen.getByLabelText("Length"), "full");
    await user.type(screen.getByLabelText("Variation (optional)"), "googly");
    await user.type(screen.getByLabelText("Notes (optional)"), "cover drives");
    await user.selectOptions(screen.getByLabelText("Session type"), "mixed");
    await user.click(screen.getByRole("button", { name: "Create session" }));
    expect(api.createSession).toHaveBeenCalledWith({
      player_id: "p2",
      date: "2026-09-28",
      session_type: "mixed",
      bowler_source: "machine",
      machine_settings: { speed_kph: 95, length: "full", variation: "googly" },
      notes: "cover drives",
    });
    expect(window.location.search).toBe("?session=s9");

    // safety gate: every item plus a name
    const confirm = await screen.findByRole("button", { name: "Confirm safety check" });
    expect(screen.getByRole("heading", { name: "Safety check" })).toBeInTheDocument();
    expect(confirm).toBeDisabled();
    await user.click(screen.getByLabelText("Batter wearing a helmet for machine batting"));
    await user.click(screen.getByLabelText("Emergency stop tested and within reach"));
    expect(confirm).toBeDisabled();
    await user.type(screen.getByLabelText("Checked by"), " Mum ");
    await user.click(confirm);
    expect(api.ackChecklist).toHaveBeenCalledWith("s9", {
      items: { helmet_on: true, estop_tested: true },
      acked_by: "Mum",
    });

    // cameras: active only, operator can drop one
    await screen.findByLabelText(/^C1/);
    expect(screen.queryByText("C3")).not.toBeInTheDocument();
    expect(screen.getByText("C2 side-on · 120 fps · 1920x1080")).toBeInTheDocument();
    await user.click(screen.getByLabelText(/^C2/));
    await user.click(screen.getByRole("button", { name: "Start recording" }));
    expect(api.start).toHaveBeenCalledWith("s9", ["C1"]);

    // recording: warnings verbatim, failed camera reported at stop
    expect(await screen.findByText("since 2026-09-28T10:01:00Z")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Incomplete data" })).toHaveTextContent(
      "C1 is registered at 60 fps, below the 120 fps floor",
    );
    await user.click(screen.getByLabelText("C1 failed"));
    await user.click(screen.getByRole("button", { name: "Stop recording" }));
    expect(api.stop).toHaveBeenCalledWith("s9", { cameras_reporting: { C1: { ok: false } } });

    // done: the server's capture verdict
    expect(await screen.findByText("Session saved")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Incomplete data" })).toHaveTextContent(
      "Missing view: C1",
    );
    expect(screen.getByRole("link", { name: "Open the session" })).toHaveAttribute(
      "href",
      "/sessions/s9",
    );
    expect(screen.getByRole("link", { name: "Start another" })).toHaveAttribute(
      "href",
      "/sessions/new",
    );
  });

  it("skips the safety check for a human bowler and stops cleanly", async () => {
    const api = fakeApi({
      start: vi.fn(async (_id: string, cameras: string[]) => ({
        state: "recording" as const,
        started_at: null,
        cameras,
        warnings: [],
        idempotent: false,
      })),
    });
    const user = userEvent.setup();
    renderFlow(api, "", "coach");
    await screen.findByLabelText("Player");
    await user.selectOptions(screen.getByLabelText("Bowler"), "human");
    expect(screen.queryByLabelText("Speed (km/h)")).not.toBeInTheDocument();
    expect(screen.queryByText("2. Safety check")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create session" }));
    expect(api.createSession).toHaveBeenCalledWith(
      expect.objectContaining({ player_id: "p1", bowler_source: "human", machine_settings: null }),
    );
    await user.click(await screen.findByRole("button", { name: "Start recording" }));
    await screen.findByRole("button", { name: "Stop recording" });
    expect(screen.queryByText(/since/)).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Incomplete data" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Stop recording" }));
    expect(api.stop).toHaveBeenCalledWith("s9", { cameras_reporting: {} });
    expect(await screen.findByText("captured")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Incomplete data" })).not.toBeInTheDocument();
  });

  it("keeps the player out of the flow", async () => {
    // no injected client: the default one is built but never called for a player
    render(<NewSessionFlow role="player" search="" />);
    expect(screen.getByText("Only a parent or coach can start a session")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Steps" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "See sessions" })).toHaveAttribute("href", "/sessions");
  });

  it("shows server refusals at each action", async () => {
    const api = fakeApi({
      createSession: vi
        .fn()
        .mockRejectedValueOnce(new ApiError(422, "unknown player_id"))
        .mockResolvedValue(CREATED),
      ackChecklist: vi
        .fn()
        .mockRejectedValueOnce(new ApiError(403, "requires one of: ['parent']"))
        .mockResolvedValue({}),
      start: vi.fn().mockRejectedValue(new ApiError(409, "safety checklist not acknowledged")),
    });
    const user = userEvent.setup();
    renderFlow(api, "", "coach");
    await fillDetails(user);
    await user.click(screen.getByRole("button", { name: "Create session" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("API 422: unknown player_id");
    await user.click(screen.getByRole("button", { name: "Create session" }));

    await user.click(await screen.findByLabelText("Batter wearing a helmet for machine batting"));
    await user.click(screen.getByLabelText("Emergency stop tested and within reach"));
    await user.type(screen.getByLabelText("Checked by"), "Coach");
    await user.click(screen.getByRole("button", { name: "Confirm safety check" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Only a parent can acknowledge the safety checklist.",
    );
    await user.click(screen.getByRole("button", { name: "Confirm safety check" }));

    await user.click(await screen.findByRole("button", { name: "Start recording" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "API 409: safety checklist not acknowledged",
    );
    await user.click(screen.getByRole("button", { name: "Back to the safety check" }));
    expect(await screen.findByRole("button", { name: "Confirm safety check" })).toBeInTheDocument();
  });

  it("offers no way back to a safety check a human session never had, and reports stop failures", async () => {
    const human: SessionOut = { ...CREATED, bowler_source: "human" };
    const api = fakeApi({
      getSession: vi.fn(async () => human),
      start: vi
        .fn()
        .mockRejectedValueOnce(new ApiError(403, "requires one of: ['parent', 'coach']"))
        .mockRejectedValueOnce(new ApiError(409, "invalid transition"))
        .mockResolvedValue({
          state: "recording",
          started_at: null,
          cameras: ["C1"],
          warnings: [],
          idempotent: false,
        }),
      stop: vi.fn().mockRejectedValue(new ApiError(500, "boom")),
    });
    const user = userEvent.setup();
    renderFlow(api, "?session=s9");
    const startButton = await screen.findByRole("button", { name: "Start recording" });
    await user.click(startButton);
    expect(await screen.findByText("Only a parent or coach can start recording.")).toBeInTheDocument();
    await user.click(startButton);
    expect(await screen.findByText("API 409: invalid transition")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Back to the safety check" })).toBeNull();
    await user.click(startButton);
    await user.click(await screen.findByRole("button", { name: "Stop recording" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("API 500: boom");
  });

  it("stops a camera selection of zero from starting", async () => {
    const human: SessionOut = { ...CREATED, bowler_source: "human" };
    const user = userEvent.setup();
    renderFlow(fakeApi({ getSession: vi.fn(async () => human) }), "?session=s9");
    await user.click(await screen.findByLabelText(/^C1/));
    await user.click(screen.getByLabelText(/^C2/));
    expect(screen.getByRole("button", { name: "Start recording" })).toBeDisabled();
  });

  it("resumes a session already on the server", async () => {
    const recording = fakeApi({ lifecycle: vi.fn(async () => lifecycle({ state: "recording" })) });
    const { unmount } = renderFlow(recording, "?session=s9");
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Stop recording" })).toBeInTheDocument();
    expect(screen.queryByText(/failed$/)).not.toBeInTheDocument();
    unmount();

    const captured = fakeApi({
      lifecycle: vi.fn(async () =>
        lifecycle({ state: "captured", degraded: true, missing_views: [] }),
      ),
    });
    const second = renderFlow(captured, "?session=s9");
    expect(await screen.findByText("Capture is degraded")).toBeInTheDocument();
    second.unmount();

    renderFlow(fakeApi(), "?session=s9");
    expect(await screen.findByRole("button", { name: "Confirm safety check" })).toBeInTheDocument();
  });

  it("retries a failed resume", async () => {
    const api = fakeApi({
      lifecycle: vi
        .fn()
        .mockRejectedValueOnce(new ApiError(404, "session not found"))
        .mockResolvedValue(lifecycle({ state: "analyzed" })),
    });
    const user = userEvent.setup();
    renderFlow(api, "?session=s9");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not resume the session: API 404: session not found",
    );
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("analyzed")).toBeInTheDocument();
  });

  it("ignores a resume that lands after unmount", async () => {
    let settle!: () => void;
    const api = fakeApi({
      lifecycle: vi.fn(
        () =>
          new Promise<LifecycleOut>((resolve) => {
            settle = () => resolve(lifecycle());
          }),
      ),
    });
    const { unmount } = renderFlow(api, "?session=s9");
    unmount();
    await act(async () => settle());
    let fail!: () => void;
    const failing = fakeApi({
      lifecycle: vi.fn(
        () =>
          new Promise<LifecycleOut>((_, reject) => {
            fail = () => reject(new Error("late"));
          }),
      ),
    });
    const second = renderFlow(failing, "?session=s9");
    second.unmount();
    await act(async () => fail());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows honest states for players, checklist and cameras", async () => {
    const user = userEvent.setup();
    const players = fakeApi({
      listPlayers: vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue([]),
    });
    const { unmount } = renderFlow(players);
    expect(await screen.findByText("Could not load players: offline")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("No players yet")).toBeInTheDocument();
    unmount();

    const checklist = fakeApi({
      machineChecklist: vi
        .fn()
        .mockRejectedValueOnce(new ApiError(500, "down"))
        .mockResolvedValue({ items: [] }),
    });
    const second = renderFlow(checklist, "?session=s9");
    expect(await screen.findByText("Could not load the safety checklist: API 500: down")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("button", { name: "Confirm safety check" })).toBeInTheDocument();
    second.unmount();

    const human: SessionOut = { ...CREATED, bowler_source: "human" };
    const cameras = fakeApi({
      getSession: vi.fn(async () => human),
      listCameras: vi
        .fn()
        .mockRejectedValueOnce(new ApiError(500, "down"))
        .mockResolvedValue([camera("C4", { active: false })]),
    });
    renderFlow(cameras, "?session=s9");
    expect(await screen.findByText("Could not load cameras: API 500: down")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("No active cameras")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open cameras" })).toHaveAttribute("href", "/cameras");
  });

  it("validates the optional fields and uses the real clock by default", async () => {
    const user = userEvent.setup();
    render(<NewSessionFlow role="parent" search="?player=nobody" api={fakeApi()} />);
    await fillDetails(user, "70");
    expect(screen.getByLabelText("Player")).toHaveValue("p1");
    await user.selectOptions(screen.getByLabelText("Player"), "p2");
    expect(screen.getByLabelText("Player")).toHaveValue("p2");
    await user.clear(screen.getByLabelText("Date"));
    await user.type(screen.getByLabelText("Variation (optional)"), "x".repeat(65));
    await user.click(screen.getByRole("button", { name: "Create session" }));
    expect(screen.getByText("Choose a date.")).toBeInTheDocument();
    expect(screen.getByText(/variation under 64/)).toBeInTheDocument();
    expect(screen.getByLabelText("Date")).toHaveAttribute("aria-invalid", "true");
  });

  it("has no axe violations on the details step", async () => {
    const { container } = renderFlow(fakeApi());
    await screen.findByLabelText("Player");
    await expectNoA11yViolations(container);
  });
});
