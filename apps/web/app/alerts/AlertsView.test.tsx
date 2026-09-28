/** Alerts inbox (US-L4): honest states, routing by role, acknowledgement. */

import { fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApiConfig } from "@/lib/api";
import { expectNoA11yViolations } from "@/test/axe";
import { createFakeApi, type FakeApi } from "@/test/fakeApi";
import { alert } from "@/test/fixtures.ops";
import { renderWithShell, type Role } from "@/test/render";
import { expectHonestStates } from "@/test/states";
import AlertsView from "./AlertsView";
import AlertsPage from "./page";

const OPEN = alert({
  id: "a1",
  code: "camera_degraded",
  severity: "warning",
  detail: { camera_id: "C3", dropped_frames: 42, extra: { fps: 118.5 } },
});
const QUIET = alert({ id: "a2", code: "disk_low", severity: "notice", session_id: null, detail: {} });
const DONE = alert({ id: "a3", code: "clock_skew", acknowledged: true, severity: "info" });

function seeded(): FakeApi {
  return createFakeApi({
    routes: {
      "/alerts?acknowledged=false": [OPEN, QUIET],
      "/alerts": [OPEN, QUIET, DONE],
    },
  });
}

function configFor(fetchFn: typeof fetch): ApiConfig {
  return { baseUrl: "http://fake.local", token: "", fetchFn };
}

function show(fetchFn: typeof fetch, role: Role | null = "parent") {
  return renderWithShell(<AlertsView config={configFor(fetchFn)} />, { role, route: "/alerts" });
}

function json(status: number, body: unknown): Response {
  return { ok: status < 300, status, statusText: "x", json: async () => body } as unknown as Response;
}

function withPost(api: FakeApi, answer: (url: string) => Response, posts: string[] = []) {
  const fetchFn: typeof fetch = async (input, init) => {
    if (init?.method === "POST") {
      posts.push(String(input));
      return answer(String(input));
    }
    return api.fetch(input, init);
  };
  return fetchFn;
}

describe("honest states", () => {
  it("covers the open inbox", async () => {
    const api = seeded();
    await expectHonestStates({
      api,
      method: "/alerts?acknowledged=false",
      empty: [],
      render: () => show(api.fetch),
      emptyText: "No open alerts",
    });
  });

  it("retries after an error", async () => {
    const api = seeded();
    api.failWith("/alerts?acknowledged=false", 500, "db offline");
    show(api.fetch);
    expect(await screen.findByRole("alert")).toHaveTextContent("db offline");
    api.reset();
    fireEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(await screen.findByTestId("alert-a1")).toBeInTheDocument();
  });

  it("says when nothing was ever routed to the role", async () => {
    const api = seeded();
    api.respondWith("/alerts", []);
    show(api.fetch, "coach");
    await screen.findByTestId("alert-a1");
    fireEvent.click(screen.getByLabelText("Show acknowledged alerts too"));
    expect(await screen.findByText("No alerts")).toBeInTheDocument();
  });
});

describe("routing by role", () => {
  it("forbids the player without a request", () => {
    const api = seeded();
    show(api.fetch, "player");
    expect(screen.getByTestId("forbidden")).toHaveTextContent("parent or coach");
    expect(api.calls).toEqual([]);
  });

  it("explains each role's inbox", async () => {
    show(seeded().fetch, "coach");
    expect(screen.getByText(/Developer alerts/)).toBeInTheDocument();
    await screen.findByTestId("alert-a1");
  });
});

describe("inbox (data parity)", () => {
  it("shows code, severity, audience, detail and session links verbatim", async () => {
    show(seeded().fetch);
    const card = await screen.findByTestId("alert-a1");
    expect(card).toHaveTextContent("camera_degraded");
    expect(within(card).getByText("warning")).toHaveAttribute("data-tone", "warning");
    expect(card).toHaveTextContent("parent");
    expect(card).toHaveTextContent("camera_idC3");
    expect(card).toHaveTextContent("dropped_frames42");
    expect(card).toHaveTextContent('extra{"fps":118.5}');
    expect(within(card).getByRole("link", { name: "Open session" })).toHaveAttribute(
      "href",
      `/sessions/${OPEN.session_id}`,
    );
    expect(within(card).getByRole("link", { name: "Pipeline runs" })).toHaveAttribute(
      "href",
      `/pipeline?session=${OPEN.session_id}`,
    );
    expect(card.querySelector("time")).toHaveAttribute("dateTime", OPEN.created_at);
    const quiet = screen.getByTestId("alert-a2");
    expect(within(quiet).getByText("notice")).toHaveAttribute("data-tone", "neutral");
    expect(within(quiet).queryByRole("link")).not.toBeInTheDocument();
  });

  it("includes acknowledged alerts on request", async () => {
    show(seeded().fetch);
    await screen.findByTestId("alert-a1");
    expect(screen.queryByTestId("alert-a3")).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Show acknowledged alerts too"));
    const done = await screen.findByTestId("alert-a3");
    expect(done).toHaveTextContent("acknowledged");
    expect(within(done).queryByRole("button", { name: /acknowledge/i })).not.toBeInTheDocument();
  });

  it("is axe clean", async () => {
    const { container } = show(seeded().fetch);
    await screen.findByTestId("alert-a1");
    await expectNoA11yViolations(container);
  });
});

describe("acknowledgement", () => {
  it("replaces the row with the server's answer", async () => {
    const posts: string[] = [];
    const api = seeded();
    show(withPost(api, () => json(200, { ...OPEN, acknowledged: true }), posts));
    const card = await screen.findByTestId("alert-a1");
    fireEvent.click(within(card).getByRole("button", { name: "Acknowledge" }));
    expect(await screen.findByText("Alert acknowledged")).toBeInTheDocument();
    expect(posts).toEqual(["http://fake.local/alerts/a1/ack"]);
    expect(within(screen.getByTestId("alert-a1")).getByText("acknowledged")).toBeInTheDocument();
    expect(screen.getByTestId("alert-a2")).toBeInTheDocument();
  });

  it("shows a 403 routing answer verbatim", async () => {
    const api = seeded();
    show(withPost(api, () => json(403, { detail: "parent cannot acknowledge developer" })));
    const card = await screen.findByTestId("alert-a1");
    fireEvent.click(within(card).getByRole("button", { name: "Acknowledge" }));
    expect(await screen.findByText("Alert not acknowledged")).toBeInTheDocument();
    expect(screen.getByText("API 403: parent cannot acknowledge developer")).toBeInTheDocument();
  });

  it("stringifies non-Error failures", async () => {
    const api = seeded();
    const fetchFn: typeof fetch = async (input, init) => {
      if (init?.method === "POST") {
        throw "offline";
      }
      return api.fetch(input, init);
    };
    show(fetchFn);
    const card = await screen.findByTestId("alert-a1");
    fireEvent.click(within(card).getByRole("button", { name: "Acknowledge" }));
    expect(await screen.findByText("offline")).toBeInTheDocument();
  });
});

describe("AlertsPage and defaults", () => {
  it("mounts the view with the shared configuration", () => {
    const element = AlertsPage();
    expect(element.type).toBe(AlertsView);
    expect(element.props).toEqual({});
  });

  it("builds its client from the shared config when none is injected", () => {
    vi.stubGlobal("fetch", () => new Promise(() => undefined));
    renderWithShell(<AlertsView />, { role: "coach" });
    expect(screen.getByRole("status")).toHaveTextContent("Loading alerts");
    vi.unstubAllGlobals();
  });
});
