/** Cameras (US-A1/I1/A4): honest states, registry verbatim, role edits, health checks. */

import { fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApiConfig } from "@/lib/api";
import { expectNoA11yViolations } from "@/test/axe";
import { createFakeApi, type FakeApi } from "@/test/fakeApi";
import { camera, checkResult, healthCheck } from "@/test/fixtures.ops";
import { renderWithShell, type Role } from "@/test/render";
import { expectHonestStates } from "@/test/states";
import CamerasView from "./CamerasView";
import CamerasPage from "./page";

const C1 = camera("C1");
const C5 = camera("C5", {
  role: null,
  reserved_for_future: true,
  protected: true,
  lens: "",
  mount: "",
  fov_reference_key: "calibration/fov/C5/era1.png",
});
const C1_OLD = camera("C1", { id: "old-c1", era_no: 1, active: false, position_label: "old spot" });
const C1_NOW = { ...C1, era_no: 2 };
const PASSED = healthCheck({ id: "h1" });
const FAILED = healthCheck({
  id: "h2",
  passed: false,
  session_id: null,
  results: { checks: [checkResult("feed:C2", { passed: false, detail: "no frames in 2 s" })] },
});
const BARE = healthCheck({ id: "h3", results: {} });

function seeded(): FakeApi {
  return createFakeApi({
    routes: {
      "/cameras": [C1, C5],
      "/cameras?include_history=true": [C1_OLD, C1_NOW, C5],
      "/health-checks": [FAILED, PASSED, BARE],
    },
  });
}

function configFor(fetchFn: typeof fetch): ApiConfig {
  return { baseUrl: "http://fake.local", token: "", fetchFn };
}

function show(fetchFn: typeof fetch, role: Role | null = "parent") {
  return renderWithShell(<CamerasView config={configFor(fetchFn)} />, { role, route: "/cameras" });
}

function json(status: number, body: unknown): Response {
  return { ok: status < 300, status, statusText: "x", json: async () => body } as unknown as Response;
}

function withPatch(api: FakeApi, answer: () => Response, bodies: unknown[] = []) {
  const fetchFn: typeof fetch = async (input, init) => {
    if (init?.method === "PATCH") {
      bodies.push({ url: String(input), body: JSON.parse(String(init.body)) });
      return answer();
    }
    return api.fetch(input, init);
  };
  return fetchFn;
}

describe("honest states", () => {
  it("covers the camera registry", async () => {
    const api = seeded();
    await expectHonestStates({
      api,
      method: "/cameras",
      empty: [],
      render: () => show(api.fetch),
      emptyText: "No cameras registered yet",
    });
  });

  it("covers the health checks", async () => {
    const api = seeded();
    await expectHonestStates({
      api,
      method: "/health-checks",
      empty: [],
      render: () => show(api.fetch),
      emptyText: "No health checks recorded yet",
    });
  });

  it("retries both lists after errors", async () => {
    const api = seeded();
    api.failWith("/cameras", 500, "registry offline");
    api.failWith("/health-checks", 500, "checks offline");
    show(api.fetch);
    expect(await screen.findByText(/registry offline/)).toBeInTheDocument();
    expect(await screen.findByText(/checks offline/)).toBeInTheDocument();
    api.reset();
    for (const button of screen.getAllByRole("button", { name: /try again/i })) {
      fireEvent.click(button);
    }
    expect(await screen.findByTestId("camera-C1-era1")).toBeInTheDocument();
    expect(await screen.findByTestId("health-h1")).toBeInTheDocument();
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

describe("registry (data parity)", () => {
  it("shows every served field, with words for blanks", async () => {
    show(seeded().fetch);
    const c1 = await screen.findByTestId("camera-C1-era1");
    expect(c1).toHaveTextContent("square leg, head height");
    expect(c1).toHaveTextContent("x 1.5 m, y -4.2 m, z 1.6 m");
    expect(c1).toHaveTextContent("1.6 m");
    expect(c1).toHaveTextContent("120 fps");
    expect(c1).toHaveTextContent("1920x1080");
    expect(c1).toHaveTextContent("wide");
    expect(c1).toHaveTextContent("tripod");
    expect(c1).toHaveTextContent("no reference image yet");
    expect(c1).toHaveTextContent("Batting side");
    const c5 = screen.getByTestId("camera-C5-era1");
    expect(c5).toHaveTextContent("reserved");
    expect(c5).toHaveTextContent("protected");
    expect(c5).toHaveTextContent("No role");
    expect(within(c5).getAllByText("not recorded")).toHaveLength(2);
    expect(c5).toHaveTextContent("calibration/fov/C5/era1.png");
  });

  it("shows past eras on request without role controls", async () => {
    show(seeded().fetch);
    await screen.findByTestId("camera-C1-era1");
    fireEvent.click(screen.getByLabelText("Show past placement eras"));
    const old = await screen.findByTestId("camera-C1-era1");
    expect(old).toHaveTextContent("past era");
    expect(old).toHaveTextContent("old spot");
    expect(within(old).queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.getByTestId("camera-C1-era2")).toHaveTextContent("active");
  });

  it("is axe clean", async () => {
    const { container } = show(seeded().fetch);
    await screen.findByTestId("health-h3");
    await expectNoA11yViolations(container);
  });
});

describe("capture role", () => {
  it("patches the role and shows the server's answer", async () => {
    const bodies: unknown[] = [];
    const api = seeded();
    show(withPatch(api, () => json(200, { ...C5, role: "bowling_side", reserved_for_future: false }), bodies));
    const select = await screen.findByLabelText("Capture role for C5");
    fireEvent.change(select, { target: { value: "bowling_side" } });
    expect(await screen.findByText("C5 role saved")).toBeInTheDocument();
    expect(bodies).toEqual([{ url: "http://fake.local/cameras/C5", body: { role: "bowling_side" } }]);
    expect(screen.getByTestId("camera-C5-era1")).not.toHaveTextContent("reserved");
  });

  it("clears a role with an explicit null", async () => {
    const bodies: unknown[] = [];
    const api = seeded();
    show(withPatch(api, () => json(200, { ...C1, role: null }), bodies));
    fireEvent.change(await screen.findByLabelText("Capture role for C1"), { target: { value: "" } });
    expect(await screen.findByText("C1 role saved")).toBeInTheDocument();
    expect(bodies).toEqual([{ url: "http://fake.local/cameras/C1", body: { role: null } }]);
  });

  it("shows a failed save verbatim and stringifies non-Errors", async () => {
    const api = seeded();
    show(withPatch(api, () => json(404, { detail: "no active config for camera" })));
    fireEvent.change(await screen.findByLabelText("Capture role for C1"), { target: { value: "wrist" } });
    expect(await screen.findByText("C1 role not saved")).toBeInTheDocument();
    expect(screen.getByText("API 404: no active config for camera")).toBeInTheDocument();
  });

  it("names a thrown non-Error", async () => {
    const api = seeded();
    const fetchFn: typeof fetch = async (input, init) => {
      if (init?.method === "PATCH") {
        throw "offline";
      }
      return api.fetch(input, init);
    };
    show(fetchFn);
    fireEvent.change(await screen.findByLabelText("Capture role for C1"), { target: { value: "wrist" } });
    expect(await screen.findByText("offline")).toBeInTheDocument();
  });
});

describe("health checks (data parity)", () => {
  it("lists records as served with every probe verbatim", async () => {
    show(seeded().fetch);
    const failed = await screen.findByTestId("health-h2");
    expect(failed).toHaveTextContent("failed");
    expect(failed).toHaveTextContent("feed:C2");
    expect(failed).toHaveTextContent("no frames in 2 s");
    expect(within(failed).queryByRole("link")).not.toBeInTheDocument();
    const passed = screen.getByTestId("health-h1");
    expect(passed).toHaveTextContent("feed:C1 ok");
    expect(within(passed).getByRole("link", { name: "Open session" })).toBeInTheDocument();
    expect(screen.getByTestId("health-h3")).toHaveTextContent("This record has no per-check detail.");
    const order = screen.getAllByTestId(/^health-/).map((item) => item.dataset.testid);
    expect(order).toEqual(["health-h2", "health-h1", "health-h3"]);
  });
});

describe("CamerasPage and defaults", () => {
  it("mounts the view with the shared configuration", () => {
    const element = CamerasPage();
    expect(element.type).toBe(CamerasView);
    expect(element.props).toEqual({});
  });

  it("builds its client from the shared config when none is injected", () => {
    vi.stubGlobal("fetch", () => new Promise(() => undefined));
    renderWithShell(<CamerasView />, { role: "parent" });
    expect(screen.getAllByRole("status").length).toBeGreaterThan(0);
    vi.unstubAllGlobals();
  });
});
