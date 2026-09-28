import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import type { ApiClient, SessionOut, SessionPage } from "@/lib/api";
import { RoleProvider, type Role } from "@/lib/auth/role";
import { expectNoA11yViolations } from "@/test/axe";
import SessionListClient, { PAGE_SIZE } from "./SessionListClient";

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
    session({
      id: "b",
      session_date: "2026-07-08",
      session_type: "bowling",
      state: "failed",
      degraded: true,
      missing_views: ["cam_side"],
    }),
  ],
  total: 2,
  limit: PAGE_SIZE,
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

function withRole(ui: ReactElement, role: Role | null = "player") {
  return render(<RoleProvider role={role}>{ui}</RoleProvider>);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("SessionListClient (US-B5 AC: filterable by date/type)", () => {
  it("loads and lists sessions as links with state and degraded reasons", async () => {
    withRole(<SessionListClient client={fakeClient()} />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading sessions");
    const list = await screen.findByRole("list", { name: "Sessions" });
    const links = within(list).getAllByRole("link");
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/sessions/a",
      "/sessions/b",
    ]);
    expect(links[0]).toHaveTextContent("2026-07-09");
    expect(links[0]).toHaveTextContent("batting · Bowling machine");
    expect(links[0]).toHaveTextContent("analyzed");
    expect(links[1]).toHaveTextContent("failed");
    expect(links[1]).toHaveTextContent("degraded");
    expect(screen.getByRole("region", { name: "Incomplete data" })).toHaveTextContent(
      "Missing view: cam_side",
    );
    expect(screen.getByTestId("sessions-count")).toHaveTextContent("Showing 1–2 of 2 sessions");
    expect(screen.queryByRole("navigation", { name: "Pages" })).not.toBeInTheDocument();
  });

  it("sends date bounds and the page size to the sessions API", async () => {
    const client = fakeClient();
    withRole(<SessionListClient client={client} />);
    await screen.findByTestId("sessions-count");
    expect(client.listSessions).toHaveBeenLastCalledWith({
      dateFrom: undefined,
      dateTo: undefined,
      limit: PAGE_SIZE,
      offset: 0,
    });
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("From"), "2026-07-01");
    await user.type(screen.getByLabelText("To"), "2026-07-09");
    await screen.findByTestId("sessions-count");
    expect(client.listSessions).toHaveBeenLastCalledWith({
      dateFrom: "2026-07-01",
      dateTo: "2026-07-09",
      limit: PAGE_SIZE,
      offset: 0,
    });
  });

  it("narrows by type client-side (the API has no type param)", async () => {
    const user = userEvent.setup();
    withRole(<SessionListClient client={fakeClient()} />);
    await screen.findByTestId("sessions-count");
    await user.selectOptions(screen.getByLabelText("Type"), "bowling");
    expect(screen.getByTestId("sessions-count")).toHaveTextContent(
      "Showing 1–2 of 2 sessions (1 bowling on this page)",
    );
    await user.selectOptions(screen.getByLabelText("Type"), "mixed");
    expect(screen.getByText("No sessions match")).toBeInTheDocument();
  });

  it("pages through the API with Previous and Next", async () => {
    const pages: Record<number, SessionPage> = {
      0: { items: [session({ id: "a" })], total: PAGE_SIZE + 1, limit: PAGE_SIZE, offset: 0 },
      [PAGE_SIZE]: {
        items: [session({ id: "z" })],
        total: PAGE_SIZE + 1,
        limit: PAGE_SIZE,
        offset: PAGE_SIZE,
      },
    };
    const client = fakeClient(async (params) => pages[params?.offset ?? 0]);
    const user = userEvent.setup();
    withRole(<SessionListClient client={client} />);
    await screen.findByTestId("sessions-count");
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("Showing 21–21 of 21 sessions")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Previous" }));
    expect(await screen.findByText("Showing 1–1 of 21 sessions")).toBeInTheDocument();
  });

  it("shows the empty state with a start action for parent and coach", async () => {
    const empty = async () => ({ items: [], total: 0, limit: PAGE_SIZE, offset: 0 });
    const { unmount } = withRole(<SessionListClient client={fakeClient(empty)} />, "parent");
    expect(await screen.findByText("No sessions yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Start a session" })).toHaveAttribute(
      "href",
      "/sessions/new",
    );
    expect(screen.getByRole("link", { name: "New session" })).toBeInTheDocument();
    unmount();
    withRole(<SessionListClient client={fakeClient(empty)} />, "player");
    expect(await screen.findByText("No sessions yet")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Start a session" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "New session" })).not.toBeInTheDocument();
  });

  it("surfaces load failures with retry, Error or not, and 403 plainly", async () => {
    let calls = 0;
    const client = fakeClient(async () => {
      calls += 1;
      if (calls === 1) throw new Error("API 500: boom");
      return PAGE;
    });
    const user = userEvent.setup();
    const { unmount } = withRole(<SessionListClient client={client} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load sessions: API 500: boom",
    );
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("list", { name: "Sessions" })).toBeInTheDocument();
    unmount();

    const forbidden = withRole(
      <SessionListClient
        client={fakeClient(async () => {
          throw new ApiError(403, "forbidden");
        })}
      />,
    );
    expect(await screen.findByText("Your role cannot see sessions.")).toBeInTheDocument();
    forbidden.unmount();

    withRole(<SessionListClient client={fakeClient(() => Promise.reject("wire down"))} />);
    expect(await screen.findByText(/wire down/)).toBeInTheDocument();
  });

  it("is axe clean", async () => {
    const { container } = withRole(<SessionListClient client={fakeClient()} />, "coach");
    await screen.findByRole("list", { name: "Sessions" });
    await expectNoA11yViolations(container);
  });

  it("ignores results and errors that land after unmount", async () => {
    let resolve!: (page: SessionPage) => void;
    const first = withRole(
      <SessionListClient client={fakeClient(() => new Promise((r) => (resolve = r)))} />,
    );
    first.unmount();
    await act(async () => {
      resolve(PAGE);
    });

    let reject!: (reason: unknown) => void;
    const second = withRole(
      <SessionListClient client={fakeClient(() => new Promise((_, r) => (reject = r)))} />,
    );
    second.unmount();
    await act(async () => {
      reject(new Error("late"));
    });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("builds a default client from the environment when none is injected", async () => {
    const fetchFn = vi.fn(
      async () =>
        new Response(JSON.stringify({ items: [], total: 0, limit: PAGE_SIZE, offset: 0 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchFn);
    withRole(<SessionListClient />);
    expect(await screen.findByText("No sessions yet")).toBeInTheDocument();
    expect(fetchFn).toHaveBeenCalledWith(
      expect.stringMatching(/\/sessions\?limit=20&offset=0$/),
      expect.anything(),
    );
  });
});
