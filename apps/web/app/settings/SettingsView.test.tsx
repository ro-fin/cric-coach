/** Settings (US-J5/L5): honest states, versions verbatim, gated new versions. */

import { fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApiConfig } from "@/lib/api";
import { expectNoA11yViolations } from "@/test/axe";
import { createFakeApi, type FakeApi } from "@/test/fakeApi";
import { appSettings } from "@/test/fixtures.ops";
import { renderWithShell, type Role } from "@/test/render";
import { expectHonestStates } from "@/test/states";
import SettingsPage from "./page";
import SettingsView from "./SettingsView";

const V1 = appSettings();
const V2 = appSettings({
  version: 2,
  approved_by: "coach",
  reason: "coach gate for the season",
  settings: {
    ...V1.settings,
    report_review: { mode: "coach_gate", timeout_hours: 12 },
  },
});
const DEFAULTS = appSettings({
  version: 0,
  approved_by: "seed",
  reason: "canonical defaults (cricai_coaching.app_settings); table unseeded",
  created_at: null,
  settings: { ...V1.settings, live_mode: { enabled: false, allowlist: [] } },
});

function seeded(): FakeApi {
  return createFakeApi({ routes: { "/settings": V2, "/settings/versions": [V1, V2] } });
}

function configFor(fetchFn: typeof fetch): ApiConfig {
  return { baseUrl: "http://fake.local", token: "", fetchFn };
}

function show(fetchFn: typeof fetch, role: Role | null = "parent") {
  return renderWithShell(<SettingsView config={configFor(fetchFn)} />, { role, route: "/settings" });
}

function json(status: number, body: unknown): Response {
  return { ok: status < 300, status, statusText: "x", json: async () => body } as unknown as Response;
}

function withPost(api: FakeApi, answer: () => Response, bodies: unknown[] = []) {
  const fetchFn: typeof fetch = async (input, init) => {
    if (init?.method === "POST") {
      bodies.push(JSON.parse(String(init.body)));
      return answer();
    }
    return api.fetch(input, init);
  };
  return fetchFn;
}

describe("honest states", () => {
  it("covers the version history", async () => {
    const api = seeded();
    await expectHonestStates({
      api,
      method: "/settings/versions",
      empty: [],
      render: () => show(api.fetch),
      emptyText: "No saved versions yet",
    });
  });

  it("retries after an error", async () => {
    const api = seeded();
    api.failWith("/settings", 500, "db offline");
    show(api.fetch);
    expect(await screen.findByRole("alert")).toHaveTextContent("db offline");
    api.reset();
    fireEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(await screen.findByTestId("settings-history")).toBeInTheDocument();
  });

  it("forbids the coach and the player without a request", () => {
    for (const role of ["coach", "player"] as const) {
      const api = seeded();
      const { unmount } = show(api.fetch, role);
      expect(screen.getByTestId("forbidden")).toHaveTextContent("This screen is for the parent.");
      expect(api.calls).toEqual([]);
      unmount();
    }
  });
});

describe("versions (data parity)", () => {
  it("shows the governing version and the history verbatim", async () => {
    show(seeded().fetch);
    expect(await screen.findByText("In force: version 2")).toBeInTheDocument();
    expect(screen.getByTestId("active-mode")).toHaveTextContent("coach_gate");
    expect(screen.getByTestId("active-timeout")).toHaveTextContent("12 hours");
    expect(screen.getByTestId("active-allowlist")).toHaveTextContent("workload_remaining_balls");
    expect(screen.getByText("coach gate for the season", { selector: "p" })).toBeInTheDocument();
    const history = screen.getByTestId("settings-history");
    expect(within(history).getByTestId("version-1")).toHaveTextContent("auto_publish");
    expect(within(history).getByTestId("version-2")).toHaveTextContent("coach_gate");
  });

  it("marks the unseeded defaults honestly", async () => {
    const api = seeded();
    api.respondWith("/settings", DEFAULTS);
    api.respondWith("/settings/versions", []);
    show(api.fetch);
    expect(await screen.findByText("canonical defaults")).toBeInTheDocument();
    expect(screen.getByText(/seed, never saved/)).toBeInTheDocument();
    expect(screen.getByText("none")).toBeInTheDocument();
    expect(screen.getByText("No saved versions yet")).toBeInTheDocument();
  });

  it("shows a review mode it does not know by its wire value", async () => {
    const api = seeded();
    api.respondWith("/settings", {
      ...V2,
      settings: { ...V2.settings, report_review: { mode: "night_gate", timeout_hours: 1 } },
    });
    show(api.fetch);
    expect(await screen.findByTestId("active-mode")).toHaveTextContent("night_gate (night_gate)");
  });

  it("is axe clean", async () => {
    const { container } = show(seeded().fetch);
    await screen.findByTestId("settings-history");
    await expectNoA11yViolations(container);
  });
});

describe("new version", () => {
  it("requires a reason and a positive deadline before posting", async () => {
    const bodies: unknown[] = [];
    show(withPost(seeded(), () => json(201, V2), bodies));
    await screen.findByTestId("settings-history");
    fireEvent.change(screen.getByLabelText("Review deadline (hours)"), { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "Save version" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("report_review.timeout_hours must be a positive number");
    expect(alert).toHaveTextContent("a reason is required for every settings change");
    expect(bodies).toEqual([]);
  });

  it("posts the whole version the parent may approve and reloads", async () => {
    const bodies: unknown[] = [];
    const api = seeded();
    const saved = appSettings({
      version: 3,
      approved_by: "parent",
      reason: "live on for nets",
      settings: {
        ...V2.settings,
        live_mode: { enabled: true, allowlist: ["ball_count", "fatigue_nudge"] },
      },
    });
    show(withPost(api, () => json(201, saved), bodies));
    await screen.findByTestId("settings-history");
    expect(screen.queryByTestId("approval-note")).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Live mode on"));
    fireEvent.change(screen.getByLabelText("Live keys allowed (one per line)"), {
      target: { value: "ball_count\nfatigue_nudge" },
    });
    fireEvent.change(screen.getByLabelText("Reason for this change"), {
      target: { value: "live on for nets" },
    });
    api.respondWith("/settings", saved);
    api.respondWith("/settings/versions", [V1, V2, saved]);
    fireEvent.click(screen.getByRole("button", { name: "Save version" }));
    expect(await screen.findByText("Settings version 3 saved")).toBeInTheDocument();
    expect(bodies).toEqual([
      {
        settings: {
          report_review: { mode: "coach_gate", timeout_hours: 12 },
          live_mode: { enabled: true, allowlist: ["ball_count", "fatigue_nudge"] },
        },
        reason: "live on for nets",
      },
    ]);
    expect(await screen.findByText("In force: version 3")).toBeInTheDocument();
    expect(screen.getByText("on", { selector: "span" })).toHaveAttribute("data-tone", "warning");
    expect(screen.getByTestId("version-3")).toHaveTextContent("on");
  });

  it("warns that a review-mode change needs the coach and shows the 403 verbatim", async () => {
    const api = seeded();
    show(
      withPost(api, () =>
        json(403, { detail: "changing report_review.mode requires coach approval (US-J5)" }),
      ),
    );
    await screen.findByTestId("settings-history");
    fireEvent.click(screen.getByLabelText("Publish reports straight away"));
    expect(screen.getByTestId("approval-note")).toHaveTextContent("needs the coach");
    fireEvent.change(screen.getByLabelText("Reason for this change"), { target: { value: "try" } });
    fireEvent.click(screen.getByRole("button", { name: "Save version" }));
    expect(await screen.findByText("Settings not saved")).toBeInTheDocument();
    expect(
      within(screen.getByRole("alert")).getByText(
        "API 403: changing report_review.mode requires coach approval (US-J5)",
      ),
    ).toBeInTheDocument();
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
    await screen.findByTestId("settings-history");
    fireEvent.change(screen.getByLabelText("Reason for this change"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Save version" }));
    expect(await screen.findAllByText("offline")).not.toHaveLength(0);
  });
});

describe("SettingsPage and defaults", () => {
  it("mounts the view with the shared configuration", () => {
    const element = SettingsPage();
    expect(element.type).toBe(SettingsView);
    expect(element.props).toEqual({});
  });

  it("builds its client from the shared config when none is injected", () => {
    vi.stubGlobal("fetch", () => new Promise(() => undefined));
    renderWithShell(<SettingsView />, { role: "parent" });
    expect(screen.getByRole("status")).toHaveTextContent("Loading settings");
    vi.unstubAllGlobals();
  });
});
