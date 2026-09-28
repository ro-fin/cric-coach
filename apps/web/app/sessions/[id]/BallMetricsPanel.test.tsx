import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, PhaseMetricsOut } from "@/lib/api";
import BallMetricsPanel from "./BallMetricsPanel";

const PHASES: PhaseMetricsOut[] = [
  {
    phase: "contact",
    metrics: {
      bat_speed: { value: 72.5, unit: "kph", confidence: 0.8, proxy: true, source: "fusion" },
      control: { value: true, unit: "", confidence: 0.9 },
    },
    schema_version: 1,
    stored: true,
  },
  {
    phase: "flight",
    metrics: {
      length_zone: { value: null, unit: "zone", confidence: 0.0, reason: "no bounce mark" },
    },
    schema_version: 1,
    stored: false,
  },
];

function fakeClient(
  ballMetrics: ApiClient["ballMetrics"] = async () => PHASES,
): ApiClient {
  return {
    listSessions: vi.fn(),
    getSession: vi.fn(),
    listTags: vi.fn(),
    listEvents: vi.fn(),
    listClips: vi.fn(),
    listSessionVideos: vi.fn(),
    ballMetrics: vi.fn(ballMetrics),
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("BallMetricsPanel (US-K1 metric summary chips)", () => {
  it("shows a loading state, then the server's phase metrics verbatim", async () => {
    render(<BallMetricsPanel sessionId="s1" ballNo={3} client={fakeClient()} />);
    expect(screen.getByTestId("metrics-loading")).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "contact (stored)" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "flight (synthesized from manual sources)" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("bat_speed: 72.50 kph · conf 0.80 · proxy · fusion"),
    ).toBeInTheDocument();
    expect(screen.getByText("control: yes · conf 0.90")).toBeInTheDocument();
    expect(
      screen.getByText("length_zone: n/a — no bounce mark · conf 0.00"),
    ).toBeInTheDocument();
  });

  it("says when a ball has no metrics", async () => {
    render(<BallMetricsPanel sessionId="s1" ballNo={4} client={fakeClient(async () => [])} />);
    expect(await screen.findByTestId("metrics-empty")).toHaveTextContent(
      "No metrics recorded for ball 4.",
    );
  });

  it("refetches when the selected ball changes", async () => {
    const client = fakeClient(async () => []);
    const view = render(<BallMetricsPanel sessionId="s1" ballNo={1} client={client} />);
    await screen.findByTestId("metrics-empty");
    view.rerender(<BallMetricsPanel sessionId="s1" ballNo={2} client={client} />);
    expect(screen.getByTestId("metrics-loading")).toBeInTheDocument();
    await screen.findByTestId("metrics-empty");
    expect(client.ballMetrics).toHaveBeenCalledTimes(2);
    expect(client.ballMetrics).toHaveBeenLastCalledWith("s1", 2);
  });

  it("surfaces API errors, message or not", async () => {
    render(
      <BallMetricsPanel
        sessionId="s1"
        ballNo={1}
        client={fakeClient(async () => {
          throw new Error("API 404: ball 1 has no ball event or tag in this session");
        })}
      />,
    );
    expect(await screen.findByTestId("metrics-error")).toHaveTextContent(
      "Metrics unavailable: API 404: ball 1 has no ball event or tag in this session",
    );
  });

  it("stringifies non-Error rejections", async () => {
    render(
      <BallMetricsPanel
        sessionId="s1"
        ballNo={1}
        client={fakeClient(() => Promise.reject("wire down"))}
      />,
    );
    expect(await screen.findByTestId("metrics-error")).toHaveTextContent("wire down");
  });

  it("ignores results and errors that land after unmount", async () => {
    let resolve!: (phases: PhaseMetricsOut[]) => void;
    const first = render(
      <BallMetricsPanel
        sessionId="s1"
        ballNo={1}
        client={fakeClient(() => new Promise((r) => (resolve = r)))}
      />,
    );
    first.unmount();
    await act(async () => {
      resolve(PHASES);
    });

    let reject!: (reason: unknown) => void;
    const second = render(
      <BallMetricsPanel
        sessionId="s1"
        ballNo={1}
        client={fakeClient(() => new Promise((_, r) => (reject = r)))}
      />,
    );
    second.unmount();
    await act(async () => {
      reject(new Error("late"));
    });
    expect(screen.queryByTestId("metrics-error")).not.toBeInTheDocument();
  });

  it("builds a default client from the environment when none is injected", async () => {
    const fetchFn = vi.fn(
      async () => new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } }),
    );
    vi.stubGlobal("fetch", fetchFn);
    render(<BallMetricsPanel sessionId="s1" ballNo={1} />);
    expect(await screen.findByTestId("metrics-empty")).toBeInTheDocument();
    expect(fetchFn).toHaveBeenCalledWith(
      "http://localhost:8000/sessions/s1/balls/1/metrics",
      expect.objectContaining({ headers: { Authorization: "Bearer " } }),
    );
  });
});
