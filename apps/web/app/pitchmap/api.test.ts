/** US-K2: pitch-map fetch helpers hit the pinned API surface, honestly, via
 * the single lib/api convention. Expected URLs and headers are built from
 * `apiBase()` and `authHeaders()`, so these tests hold before and after the
 * same-origin proxy switch (F2b). */

import { afterEach, describe, expect, it, vi } from "vitest";
import { apiBase, authHeaders } from "@/lib/api";
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
});

describe("fetch helpers", () => {
  it("fetches the session heatmap via the shared base and auth headers", async () => {
    const fetchMock = okFetch({ cells: [] });
    vi.stubGlobal("fetch", fetchMock);
    await expect(fetchSessionHeatmap("s-1")).resolves.toEqual({ cells: [] });
    expect(fetchMock).toHaveBeenCalledWith(`${apiBase()}/sessions/s-1/heatmap`, {
      headers: authHeaders(),
    });
  });

  it("fetches the session (honesty flags come with it)", async () => {
    const fetchMock = okFetch({ id: "s-1" });
    vi.stubGlobal("fetch", fetchMock);
    await fetchSession("s-1");
    expect(fetchMock).toHaveBeenCalledWith(`${apiBase()}/sessions/s-1`, {
      headers: authHeaders(),
    });
  });

  it("fetches the player (handedness drives the mirror)", async () => {
    const fetchMock = okFetch({ handedness: "left" });
    vi.stubGlobal("fetch", fetchMock);
    await expect(fetchPlayer("p-1")).resolves.toEqual({ handedness: "left" });
    expect(fetchMock).toHaveBeenCalledWith(`${apiBase()}/players/p-1`, {
      headers: authHeaders(),
    });
  });

  it("fetches the tag list for honest no-bounce accounting", async () => {
    const fetchMock = okFetch([{ ball_no: 3 }]);
    vi.stubGlobal("fetch", fetchMock);
    await expect(fetchTags("s-1")).resolves.toEqual([{ ball_no: 3 }]);
    expect(fetchMock).toHaveBeenCalledWith(`${apiBase()}/sessions/s-1/tags`, {
      headers: authHeaders(),
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
    expect(fetchMock).toHaveBeenCalledWith(`${apiBase()}/targets?session_id=s-1`, {
      headers: authHeaders(),
    });
  });

  it("returns null when targets cannot be served (endpoint ships with story i1)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 404 }));
    await expect(fetchTargets("s-1")).resolves.toBeNull();
  });
});
