import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import {
  correction,
  dailyReport,
  player,
  reportBody,
  workloadWindow,
} from "@/test/fixtures.core";
import type { PlayerOut, ReportOut, TodayApi, WindowSummaryOut } from "./api";
import { expectNoA11yViolations } from "@/test/axe";
import TodayView from "./TodayView";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return { ...actual, createTodayApi: vi.fn(() => fakeApi()) };
});

function fakeApi(
  o: {
    players?: () => Promise<PlayerOut[]>;
    reports?: (id: string) => Promise<ReportOut[]>;
    workload?: (id: string) => Promise<WindowSummaryOut[]>;
  } = {},
): TodayApi {
  return {
    listPlayers: vi.fn(o.players ?? (async () => [player()])),
    listDailyReports: vi.fn(o.reports ?? (async () => [dailyReport()])),
    workloadSummary: vi.fn(o.workload ?? (async () => [workloadWindow()])),
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

describe("TodayView", () => {
  it("shows the published report: one correction, drill, goal and evidence clips", async () => {
    render(<TodayView role="player" search="" api={fakeApi()} />);
    const correctionCard = await screen.findByRole("article", { name: "One correction" });
    expect(correctionCard).toHaveTextContent("Keep your head still over the ball at contact.");
    const clips = within(correctionCard).getAllByRole("link");
    expect(clips.map((link) => link.getAttribute("href"))).toEqual([
      "/sessions/s1?ball=7",
      "/sessions/s1?ball=12",
    ]);
    expect(screen.getByRole("article", { name: "One drill" })).toHaveTextContent(
      "Tennis-ball head-still drill, 3 sets of 12.",
    );
    const goalCard = screen.getByRole("article", { name: "One goal" });
    expect(goalCard).toHaveTextContent("Head movement (cm)");
    expect(goalCard).toHaveTextContent("4");
    expect(screen.getByText("Great balance on the front foot today.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the full report" })).toHaveAttribute(
      "href",
      "/reports?report_id=r1",
    );
    expect(screen.getByText("Arjun")).toBeInTheDocument();
  });

  it("uses the ?player= id when it is listed", async () => {
    const api = fakeApi({ players: async () => [player(), player({ id: "p2", name: "Meera" })] });
    render(<TodayView role="player" search="?player=p2" api={api} />);
    expect(await screen.findByText("Meera")).toBeInTheDocument();
    await waitFor(() => expect(api.listDailyReports).toHaveBeenCalledWith("p2"));
    await waitFor(() => expect(api.workloadSummary).toHaveBeenCalledWith("p2"));
  });

  it("renders null sections and report caveats honestly", async () => {
    const body = reportBody({
      main_correction: null,
      drill: null,
      goal: null,
      positive: "",
      honesty_banner: "Side camera was missing for 20 balls.",
      coverage_note: "40 of 60 balls analysed.",
      safety: { active: true, codes: ["pain_flag"], text: "No bowling today.", sha256: "x" },
    });
    const api = fakeApi({ reports: async () => [dailyReport({ body })] });
    render(<TodayView role="player" search="" api={api} />);
    expect(await screen.findByText("No correction today.")).toBeInTheDocument();
    expect(screen.getByText("No drill today.")).toBeInTheDocument();
    expect(screen.getByText("No goal today.")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("No bowling today.");
    const caveats = screen.getByRole("region", { name: "Incomplete data" });
    expect(caveats).toHaveTextContent("Side camera was missing for 20 balls.");
    expect(caveats).toHaveTextContent("40 of 60 balls analysed.");
  });

  it("does not raise the safety alert for an inactive verdict, and lists unlinked clips", async () => {
    const body = reportBody({
      safety: { active: false, codes: [], text: "All clear.", sha256: "x" },
      goal: { metric: "control_pct", target: null, condition: {} },
      main_correction: correction({ evidence: { "3": { cam: "c3" } } }),
    });
    const api = fakeApi({ reports: async () => [dailyReport({ body, session_id: null })] });
    render(<TodayView role="player" search="" api={api} />);
    expect(await screen.findByText("No target set")).toBeInTheDocument();
    expect(screen.queryByText("All clear.")).not.toBeInTheDocument();
    const clips = screen.getByRole("list", { name: "Evidence clips" });
    expect(clips).toHaveTextContent("Ball 3 · cam");
    expect(within(clips).queryByRole("link")).not.toBeInTheDocument();
  });

  it("survives a report body that lacks optional keys", async () => {
    const legacy = {
      kind: "daily",
      period: { start: "2026-09-27", end: "2026-09-27" },
      positive: "Good hands.",
    } as unknown as ReturnType<typeof reportBody>;
    const api = fakeApi({ reports: async () => [dailyReport({ body: legacy })] });
    render(<TodayView role="player" search="" api={api} />);
    expect(await screen.findByText("No correction today.")).toBeInTheDocument();
    expect(screen.getByText("No drill today.")).toBeInTheDocument();
    expect(screen.getByText("No goal today.")).toBeInTheDocument();
    expect(screen.getByText("Good hands.")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Incomplete data" })).not.toBeInTheDocument();
  });

  it("shows the empty report state when nothing is published", async () => {
    const api = fakeApi({ reports: async () => [dailyReport({ status: "draft" })] });
    render(<TodayView role="coach" search="" api={api} />);
    expect(await screen.findByText("No published report yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "See sessions" })).toHaveAttribute("href", "/sessions");
  });

  it("shows workload against the ceiling with verbatim numbers", async () => {
    render(<TodayView role="player" search="" api={fakeApi()} />);
    const panel = (await screen.findByText("2026-09-22 to 2026-09-28")).closest("section")!;
    expect(within(panel).getByRole("heading", { name: "Workload" })).toBeInTheDocument();
    expect(within(panel).getByText("Overs bowled").parentElement).toHaveTextContent("16of 24");
    expect(within(panel).getByText("Balls left this week").parentElement).toHaveTextContent(
      "48",
    );
    expect(within(panel).getByText("Within the safe workload")).toBeInTheDocument();
  });

  it("lists workload violations and handles a missing ceiling", async () => {
    const api = fakeApi({
      workload: async () => [
        workloadWindow({
          ceiling_overs: null,
          remaining_balls: null,
          violations: ["workload_ceiling", "pain_flag"],
        }),
      ],
    });
    render(<TodayView role="player" search="" api={api} />);
    const warnings = await screen.findByRole("list", { name: "Workload warnings" });
    expect(warnings).toHaveTextContent("Weekly bowling ceiling reached");
    expect(warnings).toHaveTextContent("Pain reported");
    expect(screen.getAllByText("No ceiling set for this age band")).toHaveLength(2);
    expect(screen.getByText("Overs bowled").parentElement).toHaveTextContent("16");
  });

  it("shows the empty workload state", async () => {
    render(<TodayView role="player" search="" api={fakeApi({ workload: async () => [] })} />);
    expect(await screen.findByText("No workload window returned")).toBeInTheDocument();
  });

  it("shows loading states with labels while reads are pending", async () => {
    const players = deferred<PlayerOut[]>();
    const reports = deferred<ReportOut[]>();
    const api = fakeApi({ players: () => players.promise, reports: () => reports.promise });
    render(<TodayView role="player" search="" api={api} />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading players");
    await act(async () => players.resolve([player()]));
    expect(screen.getByText("Loading today's report")).toBeInTheDocument();
    await act(async () => reports.resolve([]));
    expect(screen.queryByText("Loading today's report")).toBeNull();
  });

  it("shows an error with retry, and retrying loads the report", async () => {
    let calls = 0;
    const api = fakeApi({
      reports: async () => {
        calls += 1;
        if (calls === 1) throw new ApiError(500, "boom");
        return [dailyReport()];
      },
    });
    render(<TodayView role="player" search="" api={api} />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load today's report: API 500: boom");
    await userEvent.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("article", { name: "One correction" })).toBeInTheDocument();
  });

  it("says forbidden plainly on 403 and reports network failures", async () => {
    const api = fakeApi({
      reports: async () => {
        throw new ApiError(403, "forbidden");
      },
      workload: async () => {
        throw new TypeError("Failed to fetch");
      },
    });
    render(<TodayView role="player" search="" api={api} />);
    expect(await screen.findByText("Your role cannot see today's report.")).toBeInTheDocument();
    expect(
      await screen.findByText("Could not load the workload: Failed to fetch"),
    ).toBeInTheDocument();
  });

  it("reports a non-Error rejection as text", async () => {
    const api = fakeApi({ players: () => Promise.reject("offline") });
    render(<TodayView role="player" search="" api={api} />);
    expect(await screen.findByText("Could not load players: offline")).toBeInTheDocument();
  });

  it("shows the empty players state", async () => {
    render(<TodayView role="parent" search="" api={fakeApi({ players: async () => [] })} />);
    expect(await screen.findByText("No players yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Start session" })).toHaveAttribute(
      "href",
      "/sessions/new",
    );
  });

  it("offers Start session to parent and coach only", async () => {
    const { unmount } = render(<TodayView role="coach" search="" api={fakeApi()} />);
    await screen.findByText("Arjun");
    expect(screen.getByRole("link", { name: "Start session" })).toHaveAttribute(
      "href",
      "/sessions/new?player=p1",
    );
    unmount();
    render(<TodayView role="player" search="" api={fakeApi()} />);
    await screen.findByText("Arjun");
    expect(screen.queryByRole("link", { name: "Start session" })).not.toBeInTheDocument();
  });

  it("has no axe violations with a full report", async () => {
    const { container } = render(<TodayView role="parent" search="" api={fakeApi()} />);
    await screen.findByRole("article", { name: "One correction" });
    await screen.findByText("Within the safe workload");
    await expectNoA11yViolations(container);
  });

  it("builds the default API client when none is injected", async () => {
    render(<TodayView role={null} search="" />);
    expect(await screen.findByText("Arjun")).toBeInTheDocument();
  });

  it("ignores responses that land after unmount", async () => {
    const players = deferred<PlayerOut[]>();
    let rejectLate!: (reason: unknown) => void;
    const late = new Promise<PlayerOut[]>((_, reject) => {
      rejectLate = reject;
    });
    const first = render(
      <TodayView role="player" search="" api={fakeApi({ players: () => players.promise })} />,
    );
    const second = render(
      <TodayView role="player" search="" api={fakeApi({ players: () => late })} />,
    );
    first.unmount();
    second.unmount();
    await act(async () => {
      players.resolve([player()]);
      rejectLate(new Error("late"));
      await late.catch(() => undefined);
    });
    expect(screen.queryByText("Arjun")).not.toBeInTheDocument();
  });
});
