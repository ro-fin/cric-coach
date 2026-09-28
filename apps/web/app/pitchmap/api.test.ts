/** US-K2: pitch-map fetch helpers hit the pinned API surface, honestly, via
 * the single lib/api convention (NEXT_PUBLIC_API_BASE_URL + _TOKEN). */

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  fetchPlayer,
  fetchSession,
  fetchSessionHeatmap,
  fetchTags,
  fetchTargets,
} from "./api";

function okFetch(payload: unknown) {
  return vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(payload) });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("fetch helpers", () => {
  it("fetches the session heatmap via the shared LAN default base", async () => {
    const fetchMock = okFetch({ cells: [] });
    vi.stubGlobal("fetch", fetchMock);
    await expect(fetchSessionHeatmap("s-1")).resolves.toEqual({ cells: [] });
    expect(fetchMock).toHaveBeenCalledWith("http://localhost:8000/sessions/s-1/heatmap", {
      headers: {},
    });
  });

  it("prefixes the configured API base and sends the bearer token when set", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "http://lab:8000");
    vi.stubEnv("NEXT_PUBLIC_API_TOKEN", "coach-token");
    const fetchMock = okFetch({ id: "s-1" });
    vi.stubGlobal("fetch", fetchMock);
    await fetchSession("s-1");
    expect(fetchMock).toHaveBeenCalledWith("http://lab:8000/sessions/s-1", {
      headers: { Authorization: "Bearer coach-token" },
    });
  });

  it("fetches the player (handedness drives the mirror)", async () => {
    const fetchMock = okFetch({ handedness: "left" });
    vi.stubGlobal("fetch", fetchMock);
    await expect(fetchPlayer("p-1")).resolves.toEqual({ handedness: "left" });
    expect(fetchMock).toHaveBeenCalledWith("http://localhost:8000/players/p-1", { headers: {} });
  });

  it("fetches the tag list for honest no-bounce accounting", async () => {
    const fetchMock = okFetch([{ ball_no: 3 }]);
    vi.stubGlobal("fetch", fetchMock);
    await expect(fetchTags("s-1")).resolves.toEqual([{ ball_no: 3 }]);
    expect(fetchMock).toHaveBeenCalledWith("http://localhost:8000/sessions/s-1/tags", {
      headers: {},
    });
  });

  it("raises a typed error on non-2xx responses", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 404 }));
    await expect(fetchSessionHeatmap("s-404")).rejects.toThrow(
      "GET /sessions/s-404/heatmap failed: 404",
    );
  });

  it("fetches declared targets from the targets router", async () => {
    const fetchMock = okFetch([{ line: "off", length: "good", description: "top of off" }]);
    vi.stubGlobal("fetch", fetchMock);
    await expect(fetchTargets("s-1")).resolves.toEqual([
      { line: "off", length: "good", description: "top of off" },
    ]);
    expect(fetchMock).toHaveBeenCalledWith("http://localhost:8000/targets?session_id=s-1", {
      headers: {},
    });
  });

  it("returns null when targets cannot be served (endpoint ships with story i1)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 404 }));
    await expect(fetchTargets("s-1")).resolves.toBeNull();
  });
});
