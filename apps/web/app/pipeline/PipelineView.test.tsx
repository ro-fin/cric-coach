/** Pipeline screen (US-J1/L1): honest states, role gate, data parity, actions. */

import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ApiConfig } from "@/lib/api";
import { expectNoA11yViolations } from "@/test/axe";
import { createFakeApi, type FakeApi } from "@/test/fakeApi";
import { session, sessionPage } from "@/test/fixtures";
import { runDetail, runOut, runSummary, stageAttempt } from "@/test/fixtures.ops";
import { renderWithShell, type Role } from "@/test/render";
import { expectHonestStates } from "@/test/states";
import PipelineView from "./PipelineView";
import PipelinePage from "./page";

const S1 = session({ id: "s1", session_date: "2026-09-27" });
const S2 = session({
  id: "s2",
  session_date: "2026-09-20",
  state: "failed",
  degraded: true,
  missing_views: ["C3"],
});
const R1 = runSummary({ id: "r1", session_id: "s1", status: "failed", finished_at: null });
const R2 = runSummary({ id: "r2", session_id: "s1", status: "succeeded" });
const TRACE_R2 = runDetail({
  id: "r2",
  session_id: "s1",
  detector_context: { detector: "ball-yolo-v3", pose: 2 },
  stages: [
    stageAttempt("probe", { input_digest: "sha256:0123456789abcdef0123456789" }),
    stageAttempt("events", { attempt: 2, status: "failed", error: "no ball found in C2" }),
    stageAttempt("clips", {
      status: "pending",
      input_digest: null,
      started_at: null,
      finished_at: null,
    }),
  ],
});

function seeded(): FakeApi {
  return createFakeApi({
    sessions: [S1, S2],
    routes: {
      "/pipeline/sessions/s1/runs": [R1, R2],
      "/pipeline/sessions/s2/runs": [],
      "/pipeline/runs/r1": runDetail({ id: "r1", stages: [] }),
      "/pipeline/runs/r2": TRACE_R2,
    },
  });
}

function configFor(fetchFn: typeof fetch): ApiConfig {
  return { baseUrl: "http://fake.local", token: "", fetchFn };
}

function show(api: FakeApi, role: Role | null = "coach", route = "/pipeline") {
  return renderWithShell(<PipelineView config={configFor(api.fetch)} />, { role, route });
}

describe("honest states", () => {
  it("covers loading, empty, error and forbidden for the session list", async () => {
    const api = seeded();
    await expectHonestStates({
      api,
      method: "listSessions",
      empty: sessionPage([]),
      render: () => show(api),
      emptyText: "No sessions yet",
    });
  });

  it("covers loading, empty, error and forbidden for a session's runs", async () => {
    const api = seeded();
    await expectHonestStates({
      api,
      method: "/pipeline/sessions/s1/runs",
      empty: [],
      render: () => show(api),
      emptyText: "No pipeline runs for this session yet",
    });
  });

  it("retries the session list after an error", async () => {
    const api = seeded();
    api.failWith("listSessions", 500, "database offline");
    show(api);
    expect(await screen.findByRole("alert")).toHaveTextContent("database offline");
    api.reset();
    fireEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(await screen.findByTestId("runs-table")).toBeInTheDocument();
  });

  it("retries runs and the trace after errors", async () => {
    const api = seeded();
    api.failWith("/pipeline/sessions/s1/runs", 500);
    show(api);
    await screen.findByText(/Could not load runs/);
    api.reset();
    api.failWith("/pipeline/runs/r2", 500, "trace store offline");
    fireEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(await screen.findByText(/trace store offline/)).toBeInTheDocument();
    api.reset();
    fireEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(await screen.findByTestId("trace-table")).toBeInTheDocument();
  });

  it("shows the forbidden state when the trace answers 403", async () => {
    const api = seeded();
    api.failWith("/pipeline/runs/r2", 403, "requires one of: parent");
    show(api);
    expect(await screen.findByTestId("forbidden")).toHaveTextContent("requires one of: parent");
  });
});

describe("role gate", () => {
  it("shows the player a forbidden state and makes no request", () => {
    const api = seeded();
    show(api, "player");
    expect(screen.getByTestId("forbidden")).toHaveTextContent("This screen is for the parent or coach.");
    expect(api.calls).toEqual([]);
  });

  it("lets a signed-out viewer through to the API, which decides", async () => {
    const api = seeded();
    api.failWith("listSessions", 401, "missing bearer token");
    show(api, null);
    expect(await screen.findByTestId("forbidden")).toHaveTextContent("missing bearer token");
  });

  it("gives the coach no run controls", async () => {
    show(seeded(), "coach");
    await screen.findByTestId("runs-table");
    expect(screen.queryByRole("button", { name: /run pipeline/i })).not.toBeInTheDocument();
  });
});

describe("runs and trace (data parity)", () => {
  it("lists runs as served and opens the newest trace", async () => {
    show(seeded());
    const table = await screen.findByTestId("runs-table");
    expect(within(table).getByTestId("run-r1")).toHaveTextContent("failed");
    expect(within(table).getByTestId("run-r1")).toHaveTextContent("not finished");
    expect(within(table).getByTestId("run-r2")).toHaveTextContent("succeeded");
    expect(screen.getByRole("button", { name: "Showing trace" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    const trace = await screen.findByTestId("trace-table");
    expect(within(trace).getByTestId("stage-probe-1")).toHaveTextContent("sha256:0123456789ab…");
    expect(within(trace).getByTitle("sha256:0123456789abcdef0123456789")).toBeInTheDocument();
    expect(within(trace).getByTestId("stage-events-2")).toHaveTextContent("no ball found in C2");
    expect(within(trace).getByTestId("stage-clips-1")).toHaveTextContent("not started");
    expect(within(trace).getByTestId("stage-clips-1")).toHaveTextContent("none");
    expect(screen.getByTestId("detector-context")).toHaveTextContent("ball-yolo-v3");
    expect(screen.getByTestId("detector-context")).toHaveTextContent("pose2");
    const started = trace.querySelector("time");
    expect(started).toHaveAttribute("dateTime", "2026-09-27T10:00:01Z");
  });

  it("switches to an older run and says when it has no stages", async () => {
    show(seeded());
    await screen.findByTestId("trace-table");
    fireEvent.click(screen.getByRole("button", { name: "Show trace" }));
    expect(await screen.findByTestId("trace-empty")).toBeInTheDocument();
  });

  it("follows the ?session= deep link and shows its degraded banner", async () => {
    show(seeded(), "coach", "/pipeline?session=s2");
    expect(await screen.findByText("No pipeline runs for this session yet")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Incomplete data" })).toHaveTextContent(
      "missing camera view: C3",
    );
    expect(screen.getByRole("combobox")).toHaveValue("s2");
    expect(screen.getByText("The parent can start a run from this screen.")).toBeInTheDocument();
  });

  it("ignores an unknown deep link and switches sessions from the picker", async () => {
    show(seeded(), "coach", "/pipeline?session=nope");
    await screen.findByTestId("runs-table");
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "s2" } });
    expect(await screen.findByText("No pipeline runs for this session yet")).toBeInTheDocument();
    expect(window.location.search).toBe("?session=s2");
  });

  it("is axe clean with the trace open", async () => {
    const { container } = show(seeded(), "parent");
    await screen.findByTestId("trace-table");
    await expectNoA11yViolations(container);
  });
});

describe("parent actions", () => {
  function withPost(api: FakeApi, answer: () => Response): typeof fetch {
    return async (input, init) => (init?.method === "POST" ? answer() : api.fetch(input, init));
  }

  function json(status: number, body: unknown): Response {
    return {
      ok: status < 300,
      status,
      statusText: "x",
      json: async () => body,
    } as unknown as Response;
  }

  it("starts a run, toasts the outcome and opens the new trace", async () => {
    const api = seeded();
    api.seed.routes["/pipeline/runs/r9"] = runDetail({ id: "r9", stages: [stageAttempt("probe")] });
    const fetchFn = withPost(api, () => json(201, runOut({ run_id: "r9", status: "succeeded" })));
    renderWithShell(<PipelineView config={configFor(fetchFn)} />, { role: "parent" });
    await screen.findByTestId("runs-table");
    api.seed.routes["/pipeline/sessions/s1/runs"] = [R1, R2, runSummary({ id: "r9" })];
    fireEvent.click(screen.getByRole("button", { name: /run pipeline/i }));
    expect(await screen.findByText("Pipeline succeeded")).toBeInTheDocument();
    expect(screen.getByText("Run r9")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("run-r9")).toHaveTextContent("Showing trace"));
  });

  it("shows a held session's 409 detail verbatim when resuming", async () => {
    const api = seeded();
    const fetchFn = withPost(api, () =>
      json(409, { detail: "pipeline already running for this session" }),
    );
    renderWithShell(<PipelineView config={configFor(fetchFn)} />, { role: "parent" });
    await screen.findByTestId("runs-table");
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    expect(await screen.findByText("Pipeline not resumed")).toBeInTheDocument();
    expect(
      screen.getByText("API 409: pipeline already running for this session"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume" })).not.toBeDisabled();
  });

  it("names a failed trigger and stringifies non-Error failures", async () => {
    const api = seeded();
    const fetchFn: typeof fetch = async (input, init) => {
      if (init?.method === "POST") {
        throw "socket closed";
      }
      return api.fetch(input, init);
    };
    renderWithShell(<PipelineView config={configFor(fetchFn)} />, { role: "parent" });
    await screen.findByTestId("runs-table");
    fireEvent.click(screen.getByRole("button", { name: /run pipeline/i }));
    expect(await screen.findByText("Pipeline not started")).toBeInTheDocument();
    expect(screen.getByText("socket closed")).toBeInTheDocument();
  });
});

describe("PipelinePage", () => {
  it("mounts the view with the shared configuration", () => {
    const element = PipelinePage();
    expect(element.type).toBe(PipelineView);
    expect(element.props).toEqual({});
  });
});

describe("empty runs for the parent", () => {
  it("points the parent at the run control", async () => {
    show(seeded(), "parent", "/pipeline?session=s2");
    expect(await screen.findByText("Use Run pipeline to analyse it.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /run pipeline/i })).toBeEnabled();
  });
});

describe("default configuration", () => {
  it("builds its clients from the shared config when none is injected", () => {
    renderWithShell(<PipelineView />, { role: "player" });
    expect(screen.getByTestId("forbidden")).toBeInTheDocument();
  });
});
