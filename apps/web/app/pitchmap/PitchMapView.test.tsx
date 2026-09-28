/** US-K2 ST: the pitch-map view — load, mirror, overlays, cell click-through. */

import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "./api";
import { MARGIN, SVG_WIDTH } from "./geometry";
import PitchMapView from "./PitchMapView";
import { expectAxeClean } from "@/lib/testing/axe";

vi.mock("./api");

const HEATMAP: api.SessionHeatmap = {
  session_id: "s-1",
  total_balls: 3,
  cells: [
    {
      line: "off",
      length: "good",
      balls: 2,
      control_pct: 50,
      false_shot_pct: 50,
      sources: { manual: 1, auto: 1 },
      hollow: 1,
    },
    {
      line: "middle",
      length: "full",
      balls: 1,
      control_pct: null,
      false_shot_pct: null,
      sources: { manual: 0, auto: 1 },
      hollow: 0,
    },
  ],
  flagged_balls: [3],
  points: [
    {
      ball_no: 1,
      line: "off",
      length: "good",
      pitch_x: 6.5,
      pitch_y: 0.2,
      source: "manual",
      confidence: null,
      hollow: false,
    },
    {
      ball_no: 2,
      line: "off",
      length: "good",
      pitch_x: 7.0,
      pitch_y: 0.3,
      source: "auto",
      confidence: 0.4,
      hollow: true,
    },
    {
      ball_no: 3,
      line: "middle",
      length: "full",
      pitch_x: 3.0,
      pitch_y: 0.0,
      source: "auto",
      confidence: 0.9,
      hollow: false,
    },
  ],
};

const SESSION: api.SessionInfo = {
  id: "s-1",
  player_id: "p-1",
  session_date: "2026-07-01",
  degraded: false,
  missing_views: [],
};
const RH_PLAYER: api.PlayerInfo = {
  id: "p-1",
  name: "Veera",
  handedness: "right",
  is_guest: false,
};
const LH_GUEST: api.PlayerInfo = {
  id: "p-1",
  name: "Nets Guest",
  handedness: "left",
  is_guest: true,
};
// Ball 7 was tagged but never bounced (beamer / full toss / out of frame, T7).
const TAGS: api.TaggedBall[] = [{ ball_no: 1 }, { ball_no: 2 }, { ball_no: 3 }, { ball_no: 7 }];

function primeApi({
  player = RH_PLAYER,
  targets = null,
}: {
  player?: api.PlayerInfo;
  targets?: api.BowlingTarget[] | null;
} = {}) {
  vi.mocked(api.fetchSessionHeatmap).mockResolvedValue(HEATMAP);
  vi.mocked(api.fetchSession).mockResolvedValue(SESSION);
  vi.mocked(api.fetchPlayer).mockResolvedValue(player);
  vi.mocked(api.fetchTags).mockResolvedValue(TAGS);
  vi.mocked(api.fetchTargets).mockResolvedValue(targets);
}

beforeEach(() => {
  vi.resetAllMocks();
});

describe("loading and errors", () => {
  it("shows a loading state until the API answers", () => {
    vi.mocked(api.fetchSessionHeatmap).mockReturnValue(new Promise(() => {}));
    vi.mocked(api.fetchSession).mockReturnValue(new Promise(() => {}));
    vi.mocked(api.fetchTags).mockReturnValue(new Promise(() => {}));
    vi.mocked(api.fetchTargets).mockReturnValue(new Promise(() => {}));
    render(<PitchMapView sessionId="s-1" />);
    expect(screen.getByTestId("loading")).toBeInTheDocument();
  });

  it("surfaces load failures honestly", async () => {
    primeApi();
    vi.mocked(api.fetchSessionHeatmap).mockRejectedValue(new Error("boom"));
    render(<PitchMapView sessionId="s-1" />);
    expect(await screen.findByTestId("load-error")).toHaveTextContent(
      "Could not load pitch map: boom",
    );
  });

  it("stringifies non-Error failures", async () => {
    primeApi();
    vi.mocked(api.fetchTags).mockRejectedValue("nope");
    render(<PitchMapView sessionId="s-1" />);
    expect(await screen.findByTestId("load-error")).toHaveTextContent(
      "Could not load pitch map: nope",
    );
  });

  it("ignores responses that land after unmount", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    primeApi();
    let resolveHeatmap: (value: api.SessionHeatmap) => void = () => {};
    vi.mocked(api.fetchSessionHeatmap).mockReturnValue(
      new Promise((resolve) => {
        resolveHeatmap = resolve;
      }),
    );
    const { unmount } = render(<PitchMapView sessionId="s-1" />);
    unmount();
    resolveHeatmap(HEATMAP);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(errorSpy).not.toHaveBeenCalled();
    errorSpy.mockRestore();
  });

  it("ignores failures that land after unmount", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    primeApi();
    let rejectHeatmap: (reason: Error) => void = () => {};
    vi.mocked(api.fetchSessionHeatmap).mockReturnValue(
      new Promise((_resolve, reject) => {
        rejectHeatmap = reject;
      }),
    );
    const { unmount } = render(<PitchMapView sessionId="s-1" />);
    unmount();
    rejectHeatmap(new Error("late"));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(errorSpy).not.toHaveBeenCalled();
    errorSpy.mockRestore();
  });
});

describe("loaded view", () => {
  it("renders the player, honest totals, timeline link and no-bounce list", async () => {
    primeApi();
    render(<PitchMapView sessionId="s-1" />);
    expect(await screen.findByTestId("player-line")).toHaveTextContent(
      "Veera — right-hand batter",
    );
    expect(screen.getByTestId("player-line")).not.toHaveTextContent("(guest)");
    expect(screen.getByTestId("totals-line")).toHaveTextContent(
      "3 balls on the map · 1 flagged for review",
    );
    expect(screen.getByTestId("session-link")).toHaveAttribute("href", "/sessions/s-1");
    expect(screen.getByTestId("no-bounce-link-7")).toHaveAttribute(
      "href",
      "/sessions/s-1?ball=7",
    );
    expect(api.fetchPlayer).toHaveBeenCalledWith("p-1");
  });

  it("mirrors the map for a left-hand guest batter (T7)", async () => {
    primeApi({ player: LH_GUEST });
    render(<PitchMapView sessionId="s-1" />);
    expect(await screen.findByTestId("player-line")).toHaveTextContent(
      "Nets Guest — left-hand batter (guest)",
    );
    expect(screen.getByTestId("off-side-label")).toHaveAttribute("x", String(MARGIN / 4));
  });

  it("clicks through from a zone-table row to the cell's ball list", async () => {
    primeApi();
    render(<PitchMapView sessionId="s-1" />);
    fireEvent.click(await screen.findByTestId("zone-select-off-good"));
    expect(screen.getByTestId("ball-link-1")).toHaveAttribute("href", "/sessions/s-1?ball=1");
    expect(screen.getByTestId("ball-link-2")).toHaveAttribute("href", "/sessions/s-1?ball=2");
    expect(screen.queryByTestId("ball-link-3")).not.toBeInTheDocument();
  });

  it("clicks through from an SVG heat cell to the cell's ball list", async () => {
    primeApi();
    render(<PitchMapView sessionId="s-1" />);
    fireEvent.click(await screen.findByTestId("cell-middle-full"));
    expect(screen.getByTestId("ball-link-3")).toHaveAttribute("href", "/sessions/s-1?ball=3");
  });
});

describe("overlay toggles and frames", () => {
  it("toggles control coloring, bounce points and the legend", async () => {
    primeApi();
    render(<PitchMapView sessionId="s-1" />);
    expect(await screen.findByTestId("legend-line")).toHaveTextContent("more balls in that cell");
    fireEvent.click(screen.getByLabelText("control coloring"));
    expect(screen.getByTestId("legend-line")).toHaveTextContent("control %");
    expect(screen.getByTestId("cell-middle-full")).toHaveClass("fill-border");
    expect(screen.getByTestId("point-1")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("bounce points"));
    expect(screen.queryByTestId("point-1")).not.toBeInTheDocument();
  });

  it("reports declared-target unavailability honestly (targets API not serving)", async () => {
    primeApi({ targets: null });
    render(<PitchMapView sessionId="s-1" />);
    fireEvent.click(await screen.findByLabelText("target zones"));
    expect(screen.getByTestId("targets-unavailable")).toBeInTheDocument();
  });

  it("distinguishes 'no targets declared' from 'unavailable'", async () => {
    primeApi({ targets: [] });
    render(<PitchMapView sessionId="s-1" />);
    fireEvent.click(await screen.findByLabelText("target zones"));
    expect(screen.getByTestId("targets-empty")).toBeInTheDocument();
    expect(screen.queryByTestId("targets-unavailable")).not.toBeInTheDocument();
  });

  it("outlines declared targets when present", async () => {
    primeApi({
      targets: [{ line: "off", length: "good", description: "top of off" }],
    });
    render(<PitchMapView sessionId="s-1" />);
    fireEvent.click(await screen.findByLabelText("target zones"));
    expect(screen.getByTestId("target-off-good")).toBeInTheDocument();
    expect(screen.queryByTestId("targets-empty")).not.toBeInTheDocument();
  });

  it("switches between batting-end and bowling-end frames", async () => {
    primeApi();
    render(<PitchMapView sessionId="s-1" />);
    expect(await screen.findByText("bowler end (far)")).toBeInTheDocument();
    expect(screen.getByTestId("off-side-label")).toHaveAttribute(
      "x",
      String(SVG_WIDTH - MARGIN / 4),
    );
    fireEvent.click(screen.getByLabelText("bowling end (batter far)"));
    expect(screen.getByText("batter end (far)")).toBeInTheDocument();
    expect(screen.getByTestId("off-side-label")).toHaveAttribute("x", String(MARGIN / 4));
    fireEvent.click(screen.getByLabelText("batting end (bowler far)"));
    expect(screen.getByText("bowler end (far)")).toBeInTheDocument();
  });
});

describe("honest states on the primitives", () => {
  it("labels the skeleton for screen readers", () => {
    vi.mocked(api.fetchSessionHeatmap).mockReturnValue(new Promise(() => {}));
    vi.mocked(api.fetchSession).mockReturnValue(new Promise(() => {}));
    vi.mocked(api.fetchTags).mockReturnValue(new Promise(() => {}));
    vi.mocked(api.fetchTargets).mockReturnValue(new Promise(() => {}));
    render(<PitchMapView sessionId="s-1" />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading pitch map");
  });

  it("retries after a failure and then renders the map", async () => {
    primeApi();
    vi.mocked(api.fetchSessionHeatmap).mockRejectedValueOnce(new Error("offline"));
    render(<PitchMapView sessionId="s-1" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("offline");
    fireEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(await screen.findByTestId("player-line")).toBeInTheDocument();
    expect(api.fetchSessionHeatmap).toHaveBeenCalledTimes(2);
  });

  it("shows the empty state with a way back when nothing was tagged or placed", async () => {
    primeApi();
    vi.mocked(api.fetchSessionHeatmap).mockResolvedValue({
      ...HEATMAP,
      total_balls: 0,
      cells: [],
      points: [],
      flagged_balls: [],
    });
    vi.mocked(api.fetchTags).mockResolvedValue([]);
    render(<PitchMapView sessionId="s-1" />);
    const empty = await screen.findByTestId("pitchmap-empty");
    expect(empty).toHaveTextContent("No balls on the map yet");
    expect(screen.getAllByTestId("session-link")).toHaveLength(2);
    expect(screen.queryByTestId("pitch-map-svg")).not.toBeInTheDocument();
  });

  it("keeps the map for tagged balls without bounces", async () => {
    primeApi();
    vi.mocked(api.fetchSessionHeatmap).mockResolvedValue({
      ...HEATMAP,
      total_balls: 0,
      cells: [],
      points: [],
      flagged_balls: [],
    });
    render(<PitchMapView sessionId="s-1" />);
    expect(await screen.findByTestId("zone-table-empty")).toBeInTheDocument();
    expect(screen.getByTestId("no-bounce-link-7")).toBeInTheDocument();
  });

  it("shows missing camera views verbatim in the degraded banner", async () => {
    primeApi();
    vi.mocked(api.fetchSession).mockResolvedValue({
      ...SESSION,
      degraded: true,
      missing_views: ["C3"],
    });
    render(<PitchMapView sessionId="s-1" />);
    const banner = await screen.findByRole("region", { name: "Incomplete data" });
    expect(banner).toHaveTextContent("missing camera view: C3");
  });

  it("shows the degraded banner on the empty state too", async () => {
    primeApi();
    vi.mocked(api.fetchSession).mockResolvedValue({ ...SESSION, degraded: true });
    vi.mocked(api.fetchSessionHeatmap).mockResolvedValue({ ...HEATMAP, total_balls: 0 });
    vi.mocked(api.fetchTags).mockResolvedValue([]);
    render(<PitchMapView sessionId="s-1" />);
    await screen.findByTestId("pitchmap-empty");
    expect(screen.getByRole("region", { name: "Incomplete data" })).toHaveTextContent(
      "session is marked degraded",
    );
  });

  it("marks a guest batter with a badge and has no axe violations", async () => {
    primeApi({ player: LH_GUEST, targets: [] });
    const { container } = render(<PitchMapView sessionId="s-1" />);
    await screen.findByTestId("player-line");
    expect(screen.getByText("guest")).toBeInTheDocument();
    await expectAxeClean(container);
  });
});
