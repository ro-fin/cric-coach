/** US-K4: the progress dashboard's four honest states through the shared H1
 * harness, driving the real feature client with the fake API's fetch. */

import { cleanup } from "@testing-library/react";
import { afterEach, describe, it, vi } from "vitest";
import { createFakeApi } from "@/test/fakeApi";
import { renderWithShell } from "@/test/render";
import { expectHonestStates } from "@/test/states";
import { DEFAULT_CONFIG } from "./api";
import { COPY } from "./copy";
import ProgressPage from "./page";

const BASE = DEFAULT_CONFIG.base;

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ProgressPage honest states (H1 harness)", () => {
  it("covers loading, empty, error and forbidden for the weekly reports", async () => {
    const api = createFakeApi({
      routes: {
        [`${BASE}/reports`]: [],
        [`${BASE}/milestones/players/p1`]: [],
        [`${BASE}/workload/players/p1/summary`]: [],
        [`${BASE}/wellness/p1/state`]: { pain_active: false },
      },
    });
    vi.stubGlobal("fetch", api.fetch);
    await expectHonestStates({
      api,
      method: `${BASE}/reports`,
      empty: [],
      render: () => renderWithShell(<ProgressPage />, { role: "parent", route: "/progress?player=p1" }),
      emptyText: COPY.noReports,
    });
  });
});
