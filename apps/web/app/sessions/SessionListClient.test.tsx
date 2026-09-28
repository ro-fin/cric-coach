import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, SessionOut, SessionPage } from "@/lib/api";
import SessionListClient from "./SessionListClient";

function session(overrides: Partial<SessionOut> = {}): SessionOut {
  return {
    id: "s1",
    player_id: "p1",
    session_date: "2026-07-09",
    session_type: "batting",
    bowler_source: "machine",
    machine_settings: null,
    notes: null,
    state: "analyzed",
    degraded: false,
    missing_views: [],
    ...overrides,
  };
}

const PAGE: SessionPage = {
  items: [
    session({ id: "a", session_date: "2026-07-09", session_type: "batting" }),
    session({ id: "b", session_date: "2026-07-08", session_type: "bowling" }),
  ],
  total: 2,
  limit: 50,
  offset: 0,
};

function fakeClient(listSessions: ApiClient["listSessions"] = async () => PAGE): ApiClient {
  return {
    listSessions: vi.fn(listSessions),
    getSession: vi.fn(),
    listTags: vi.fn(),
    listEvents: vi.fn(),
    listClips: vi.fn(),
    listSessionVideos: vi.fn(),
    ballMetrics: vi.fn(),
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("SessionListClient (US-B5 AC: filterable by date/type)", () => {
  it("loads and lists sessions as links to their detail pages", async () => {
    render(<SessionListClient client={fakeClient()} />);
    expect(screen.getByTestId("sessions-loading")).toBeInTheDocument();
    const link = await screen.findByRole("link", {
      name: "2026-07-09 · batting · machine · analyzed",
    });
    expect(link).toHaveAttribute("href", "/sessions/a");
    expect(screen.getByTestId("sessions-count")).toHaveTextContent("2 of 2 sessions");
  });

  it("sends date bounds to the sessions API", async () => {
    const client = fakeClient();
    render(<SessionListClient client={client} />);
    await screen.findByTestId("sessions-count");
    expect(client.listSessions).toHaveBeenLastCalledWith({
      dateFrom: undefined,
      dateTo: undefined,
    });
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("From"), "2026-07-01");
    await user.type(screen.getByLabelText("To"), "2026-07-09");
    await screen.findByTestId("sessions-count");
    expect(client.listSessions).toHaveBeenLastCalledWith({
      dateFrom: "2026-07-01",
      dateTo: "2026-07-09",
    });
  });

  it("narrows by type client-side (the API has no type param)", async () => {
    const user = userEvent.setup();
    render(<SessionListClient client={fakeClient()} />);
    await screen.findByTestId("sessions-count");
    await user.selectOptions(screen.getByLabelText("Type"), "bowling");
    expect(screen.getByTestId("sessions-count")).toHaveTextContent("1 of 2 sessions");
    expect(screen.queryByText(/2026-07-09 · batting/)).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Type"), "mixed");
    expect(screen.getByTestId("sessions-empty")).toBeInTheDocument();
  });

  it("surfaces load failures, Error or not", async () => {
    render(
      <SessionListClient
        client={fakeClient(async () => {
          throw new Error("API 401: missing bearer token");
        })}
      />,
    );
    expect(await screen.findByTestId("sessions-error")).toHaveTextContent(
      "Could not load sessions: API 401: missing bearer token",
    );
    render(<SessionListClient client={fakeClient(() => Promise.reject("wire down"))} />);
    expect(await screen.findByText(/wire down/)).toBeInTheDocument();
  });

  it("ignores results and errors that land after unmount", async () => {
    let resolve!: (page: SessionPage) => void;
    const first = render(
      <SessionListClient client={fakeClient(() => new Promise((r) => (resolve = r)))} />,
    );
    first.unmount();
    await act(async () => {
      resolve(PAGE);
    });

    let reject!: (reason: unknown) => void;
    const second = render(
      <SessionListClient client={fakeClient(() => new Promise((_, r) => (reject = r)))} />,
    );
    second.unmount();
    await act(async () => {
      reject(new Error("late"));
    });
    expect(screen.queryByTestId("sessions-error")).not.toBeInTheDocument();
  });

  it("builds a default client from the environment when none is injected", async () => {
    const fetchFn = vi.fn(
      async () =>
        new Response(JSON.stringify({ items: [], total: 0, limit: 50, offset: 0 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchFn);
    render(<SessionListClient />);
    expect(await screen.findByTestId("sessions-empty")).toBeInTheDocument();
    expect(fetchFn).toHaveBeenCalledWith("http://localhost:8000/sessions", expect.anything());
  });
});
