/** Wellness screen (US-H4): honest states, state verbatim, check-in, adult clearance. */

import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApiConfig } from "@/lib/api";
import { expectNoA11yViolations } from "@/test/axe";
import { createFakeApi, type FakeApi } from "@/test/fakeApi";
import { checkin, clearance, wellnessState } from "@/test/fixtures.ops";
import { renderWithShell, type Role } from "@/test/render";
import { expectHonestStates } from "@/test/states";
import { WELLNESS_COPY as COPY } from "./copy";
import WellnessPage from "./page";
import WellnessView from "./WellnessView";

const ARJUN = { id: "p1", name: "Arjun", birthdate: "2014-11-20", handedness: "right", is_guest: false };
const MEERA = { id: "p2", name: "Meera", birthdate: "2014-03-02", handedness: "left", is_guest: false };

const CALM = checkin({
  id: "c1",
  checkin_date: "2026-09-26",
  energy: 4,
  sleep_hours: 8.5,
  soreness: { foot_left: 1, neck: 2 },
});
const SORE = checkin({
  id: "c2",
  checkin_date: "2026-09-27",
  energy: null,
  sleep_hours: null,
  soreness: { knee_left: 7 },
  pain: true,
  pain_note: "Elbow hurts after bowling",
  created_by: "player",
});

function seeded(): FakeApi {
  return createFakeApi({
    routes: {
      "/players": [ARJUN, MEERA],
      "/wellness/p1/state": wellnessState({
        pain_active: true,
        bowling_suppressed: true,
        escalation: true,
        pain_reports_in_window: 3,
        days_since_checkin: 1,
        checked_in: false,
        open_pain_checkin_ids: ["c2"],
      }),
      "/wellness/p1/checkins": [CALM, SORE],
      "/wellness/p2/state": wellnessState({ days_since_checkin: null, last_checkin_date: null }),
      "/wellness/p2/checkins": [],
    },
  });
}

function configFor(fetchFn: typeof fetch): ApiConfig {
  return { baseUrl: "http://fake.local", token: "", fetchFn };
}

function show(api: FakeApi | typeof fetch, role: Role | null = "parent", route = "/wellness") {
  const fetchFn = typeof api === "function" ? api : api.fetch;
  return renderWithShell(<WellnessView config={configFor(fetchFn)} />, { role, route });
}

function json(status: number, body: unknown): Response {
  return { ok: status < 300, status, statusText: "x", json: async () => body } as unknown as Response;
}

/** Answer POSTs with `answer`, everything else from the fake; record POST bodies. */
function withPost(api: FakeApi, answer: (url: string) => Response, posts: unknown[] = []) {
  const fetchFn: typeof fetch = async (input, init) => {
    if (init?.method === "POST") {
      posts.push({ url: String(input), body: JSON.parse(String(init.body)) });
      return answer(String(input));
    }
    return api.fetch(input, init);
  };
  return fetchFn;
}

describe("honest states", () => {
  it("covers the player list", async () => {
    const api = seeded();
    await expectHonestStates({
      api,
      method: "/players",
      empty: [],
      render: () => show(api),
      emptyText: COPY.noPlayersTitle,
    });
  });

  it("covers the player's check-ins", async () => {
    const api = seeded();
    await expectHonestStates({
      api,
      method: "/wellness/p1/checkins",
      empty: [],
      render: () => show(api),
      emptyText: COPY.historyEmptyTitle,
    });
  });

  it("retries players and wellness data after errors", async () => {
    const api = seeded();
    api.failWith("/players", 500, "db offline");
    show(api);
    expect(await screen.findByRole("alert")).toHaveTextContent("db offline");
    api.reset();
    api.failWith("/wellness/p1/state", 500, "state offline");
    fireEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(await screen.findByText(/state offline/)).toBeInTheDocument();
    api.reset();
    fireEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(await screen.findByTestId("wellness-flags")).toBeInTheDocument();
  });

  it("shows forbidden when the state answers 403", async () => {
    const api = seeded();
    api.failWith("/wellness/p1/state", 403, "requires one of: parent");
    show(api);
    expect(await screen.findByTestId("forbidden")).toHaveTextContent("requires one of: parent");
  });
});

describe("state and history (data parity)", () => {
  it("renders the state machine's fields verbatim", async () => {
    show(seeded());
    const flags = await screen.findByTestId("wellness-flags");
    expect(flags).toHaveTextContent(COPY.painActive);
    expect(flags).toHaveTextContent(COPY.bowlingPaused);
    expect(flags).toHaveTextContent(COPY.escalation);
    expect(screen.getByTestId("days-since")).toHaveTextContent("1");
    expect(screen.getByTestId("pain-reports")).toHaveTextContent("3");
    expect(screen.getByText(COPY.notCheckedIn)).toBeInTheDocument();
  });

  it("lists check-ins newest first with honest gaps", async () => {
    show(seeded());
    await screen.findByTestId("checkin-c2");
    const items = screen.getAllByTestId(/^checkin-/);
    expect(items.map((item) => item.dataset.testid)).toEqual(["checkin-c2", "checkin-c1"]);
    const sore = screen.getByTestId("checkin-c2");
    expect(sore).toHaveTextContent("energy not recorded · sleep not recorded");
    expect(sore).toHaveTextContent("Elbow hurts after bowling");
    expect(sore).toHaveTextContent(COPY.openBadge);
    expect(sore).toHaveTextContent("knee left 7");
    expect(sore).toHaveTextContent("by player");
    const calm = screen.getByTestId("checkin-c1");
    expect(calm).toHaveTextContent("energy 4 · sleep 8.5h");
    expect(calm).toHaveTextContent("neck sore, foot left a little");
    expect(calm).not.toHaveTextContent(COPY.painBadge);
  });

  it("says when a player has never checked in and switches players", async () => {
    show(seeded(), "player", "/wellness?player=p2");
    expect(await screen.findByText(COPY.neverCheckedIn)).toBeInTheDocument();
    expect(screen.getByText(COPY.noPain)).toBeInTheDocument();
    expect(screen.getByText(COPY.checkedIn)).toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox", { name: COPY.pickPlayer }), {
      target: { value: "p1" },
    });
    expect(await screen.findByTestId("checkin-c2")).toBeInTheDocument();
    expect(window.location.search).toBe("?player=p1");
  });

  it("hides the player picker when there is only one player", async () => {
    const api = seeded();
    api.respondWith("/players", [ARJUN]);
    show(api);
    await screen.findByTestId("wellness-flags");
    expect(screen.queryByRole("combobox", { name: COPY.pickPlayer })).not.toBeInTheDocument();
  });

  it("is axe clean", async () => {
    const { container } = show(seeded());
    await screen.findByTestId("checkin-c2");
    await expectNoA11yViolations(container);
  });
});

describe("check-in form", () => {
  it("explains out-of-range values before posting", async () => {
    const posts: unknown[] = [];
    const api = seeded();
    show(withPost(api, () => json(201, checkin()), posts));
    await screen.findByTestId("wellness-flags");
    fireEvent.change(screen.getByLabelText(COPY.sleep), { target: { value: "20" } });
    fireEvent.click(screen.getByRole("button", { name: COPY.submit }));
    expect(await screen.findByText("sleep_hours must be 0.0..14.0, got 20")).toBeInTheDocument();
    expect(posts).toEqual([]);
  });

  it("posts the check-in body and reloads the history", async () => {
    const posts: { url: string; body: unknown }[] = [];
    const api = seeded();
    show(withPost(api, () => json(201, checkin({ id: "c9" })), posts));
    await screen.findByTestId("wellness-flags");
    fireEvent.change(screen.getByLabelText(COPY.date), { target: { value: "2026-09-25" } });
    fireEvent.click(screen.getByRole("radio", { name: "3" }));
    fireEvent.change(screen.getByLabelText(COPY.sleep), { target: { value: "7.5" } });
    fireEvent.change(screen.getByLabelText(COPY.sorenessArea), { target: { value: "elbow_right" } });
    fireEvent.change(screen.getByLabelText(COPY.sorenessLevel), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: COPY.addSoreness }));
    fireEvent.change(screen.getByLabelText(COPY.sorenessArea), { target: { value: "neck" } });
    fireEvent.click(screen.getByRole("button", { name: COPY.addSoreness }));
    fireEvent.click(screen.getByRole("button", { name: "Remove neck" }));
    fireEvent.click(screen.getByLabelText(COPY.pain));
    fireEvent.change(screen.getByLabelText(COPY.painNote), { target: { value: "right elbow" } });
    api.seed.routes["/wellness/p1/checkins"] = [CALM, SORE, checkin({ id: "c9" })];
    fireEvent.click(screen.getByRole("button", { name: COPY.submit }));
    expect(await screen.findByText(COPY.saved)).toBeInTheDocument();
    expect(posts).toEqual([
      {
        url: "http://fake.local/wellness/p1/checkins",
        body: {
          checkin_date: "2026-09-25",
          soreness: { elbow_right: 2 },
          energy: 3,
          sleep_hours: 7.5,
          pain: true,
          pain_note: "right elbow",
        },
      },
    ]);
    expect(await screen.findByTestId("checkin-c9")).toBeInTheDocument();
    expect(screen.getByText(COPY.noSoreness)).toBeInTheDocument();
  });

  it("shows the server's 422 problems verbatim", async () => {
    const api = seeded();
    show(
      withPost(api, () =>
        json(422, { detail: ["unknown soreness body key: 'tail'", "energy must be 1..5, got 0"] }),
      ),
    );
    await screen.findByTestId("wellness-flags");
    fireEvent.click(screen.getByRole("button", { name: COPY.submit }));
    const alerts = await screen.findAllByText(
      "API 422: unknown soreness body key: 'tail'; energy must be 1..5, got 0",
    );
    expect(alerts.length).toBeGreaterThan(0);
    expect(screen.getByText(COPY.notSaved)).toBeInTheDocument();
  });

  it("stringifies a non-Error failure", async () => {
    const api = seeded();
    const fetchFn: typeof fetch = async (input, init) => {
      if (init?.method === "POST") {
        throw "offline";
      }
      return api.fetch(input, init);
    };
    show(fetchFn);
    await screen.findByTestId("wellness-flags");
    fireEvent.click(screen.getByRole("button", { name: COPY.submit }));
    expect(await screen.findAllByText("offline")).not.toHaveLength(0);
  });
});

describe("adult pain clearance", () => {
  it("is not offered to the player", async () => {
    show(seeded(), "player");
    await screen.findByTestId("checkin-c2");
    expect(screen.queryByRole("button", { name: COPY.clear })).not.toBeInTheDocument();
  });

  it("requires a note, then posts it and reloads", async () => {
    const posts: { url: string; body: unknown }[] = [];
    const api = seeded();
    show(withPost(api, () => json(201, clearance()), posts), "coach");
    fireEvent.click(await screen.findByRole("button", { name: COPY.clear }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("2026-09-27 — Elbow hurts after bowling");
    fireEvent.click(within(dialog).getByRole("button", { name: COPY.clearConfirm }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent(COPY.clearNoteRequired);
    expect(posts).toEqual([]);
    fireEvent.change(within(dialog).getByLabelText(COPY.clearNote), {
      target: { value: "  Rested, throws without pain  " },
    });
    api.seed.routes["/wellness/p1/state"] = wellnessState();
    fireEvent.click(within(dialog).getByRole("button", { name: COPY.clearConfirm }));
    expect(await screen.findByText(COPY.clearDone)).toBeInTheDocument();
    expect(posts).toEqual([
      {
        url: "http://fake.local/wellness/p1/checkins/c2/clearance",
        body: { note: "Rested, throws without pain" },
      },
    ]);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await screen.findByText(COPY.noPain)).toBeInTheDocument();
  });

  it("shows a 409 verbatim and can be cancelled", async () => {
    const api = seeded();
    show(withPost(api, () => json(409, { detail: "pain flag already cleared" })), "parent");
    fireEvent.click(await screen.findByRole("button", { name: COPY.clear }));
    const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(COPY.clearNote), { target: { value: "ok" } });
    fireEvent.click(within(dialog).getByRole("button", { name: COPY.clearConfirm }));
    expect(await within(dialog).findByText("API 409: pain flag already cleared")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: COPY.clearCancel }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("closes on Escape and stringifies non-Error failures", async () => {
    const api = seeded();
    const fetchFn: typeof fetch = async (input, init) => {
      if (init?.method === "POST") {
        throw "socket closed";
      }
      return api.fetch(input, init);
    };
    show(fetchFn, "parent");
    fireEvent.click(await screen.findByRole("button", { name: COPY.clear }));
    const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(COPY.clearNote), { target: { value: "ok" } });
    fireEvent.click(within(dialog).getByRole("button", { name: COPY.clearConfirm }));
    expect(await within(dialog).findByText("socket closed")).toBeInTheDocument();
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("clears a pain check-in that has no note", async () => {
    const api = seeded();
    api.seed.routes["/wellness/p1/checkins"] = [{ ...SORE, pain_note: null }];
    show(api, "parent");
    fireEvent.click(await screen.findByRole("button", { name: COPY.clear }));
    expect(screen.getByRole("dialog")).toHaveTextContent("2026-09-27");
    expect(screen.getByRole("dialog")).not.toHaveTextContent("—");
  });
});

describe("WellnessPage and defaults", () => {
  it("mounts the view with the shared configuration", () => {
    const element = WellnessPage();
    expect(element.type).toBe(WellnessView);
    expect(element.props).toEqual({});
  });

  it("builds its client from the shared config when none is injected", () => {
    vi.stubGlobal("fetch", () => new Promise(() => undefined));
    renderWithShell(<WellnessView />, { role: "player" });
    vi.unstubAllGlobals();
    expect(screen.getByRole("status")).toHaveTextContent(COPY.loading);
  });
});
