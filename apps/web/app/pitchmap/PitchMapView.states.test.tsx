/** US-K2: the pitch map's four honest states through the shared H1 harness,
 * driving the real feature client with the fake API's fetch. */

import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiBase } from "@/lib/api";
import { createFakeApi } from "@/test/fakeApi";
import { session } from "@/test/fixtures";
import { renderWithShell } from "@/test/render";
import { expectHonestStates } from "@/test/states";
import PitchMapView from "./PitchMapView";

const BASE = apiBase();

function seeded() {
  return createFakeApi({
    routes: {
      [`${BASE}/sessions/s1/heatmap`]: {
        session_id: "s1",
        total_balls: 0,
        cells: [],
        flagged_balls: [],
        points: [],
      },
      [`${BASE}/sessions/s1`]: session({ id: "s1", player_id: "p1" }),
      [`${BASE}/sessions/s1/tags`]: [{ ball_no: 1 }],
      [`${BASE}/players/p1`]: { id: "p1", name: "Arjun", handedness: "right", is_guest: false },
      [`${BASE}/targets`]: [],
    },
  });
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("PitchMapView honest states (H1 harness)", () => {
  it("covers loading, empty, error and forbidden for the session's tags", async () => {
    const api = seeded();
    vi.stubGlobal("fetch", api.fetch);
    await expectHonestStates({
      api,
      method: `${BASE}/sessions/s1/tags`,
      empty: [],
      render: () => renderWithShell(<PitchMapView sessionId="s1" />, { role: "player" }),
      emptyText: "No balls on the map yet",
    });
  });

  it("keeps the map when tagged balls have no bounce point", async () => {
    vi.stubGlobal("fetch", seeded().fetch);
    renderWithShell(<PitchMapView sessionId="s1" />);
    expect(await screen.findByTestId("no-bounce-link-1")).toBeInTheDocument();
  });
});
