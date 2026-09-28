/** US-K3: notes fetch helpers — URL building, shared auth convention, error
 * surfacing. Base and bearer come from lib/api (NEXT_PUBLIC_API_BASE_URL +
 * NEXT_PUBLIC_API_TOKEN); the old localStorage token path is gone — nothing
 * ever wrote it, so it could never authenticate a real device. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createNote, deleteNote, listNotes } from "./api";

const fetchMock = vi.fn();

function ok(payload: unknown): Response {
  return { ok: true, json: async () => payload } as unknown as Response;
}

function fail(status: number): Response {
  return { ok: false, status } as unknown as Response;
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("listNotes", () => {
  it("queries by player only when no filters are set (shared LAN base, no token)", async () => {
    fetchMock.mockResolvedValue(ok([]));
    await expect(listNotes({ playerId: "p1" })).resolves.toEqual([]);
    expect(fetchMock).toHaveBeenCalledWith("http://localhost:8000/notes?player_id=p1", {
      headers: {},
    });
  });

  it("adds session, ball and search filters and the env bearer token", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_TOKEN", "tok-1");
    fetchMock.mockResolvedValue(ok([{ id: "n1" }]));
    const notes = await listNotes({ playerId: "p1", sessionId: "s1", ballNo: 7, q: "pull" });
    expect(notes).toEqual([{ id: "n1" }]);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/notes?player_id=p1&session_id=s1&ball_no=7&q=pull",
      { headers: { Authorization: "Bearer tok-1" } },
    );
  });

  it("throws on a non-ok response", async () => {
    fetchMock.mockResolvedValue(fail(403));
    await expect(listNotes({ playerId: "p1" })).rejects.toThrow("notes list failed: 403");
  });
});

describe("createNote", () => {
  it("posts the draft as JSON with the env bearer token", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_TOKEN", "tok-1");
    fetchMock.mockResolvedValue(ok({ id: "n1" }));
    const draft = { player_id: "p1", body: "Watch the elbow.", visibility: "shared" as const };
    await expect(createNote(draft)).resolves.toEqual({ id: "n1" });
    expect(fetchMock).toHaveBeenCalledWith("http://localhost:8000/notes", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer tok-1" },
      body: JSON.stringify(draft),
    });
  });

  it("throws on a non-ok response", async () => {
    fetchMock.mockResolvedValue(fail(422));
    await expect(
      createNote({ player_id: "p1", body: "x", visibility: "coach_only" }),
    ).rejects.toThrow("note create failed: 422");
  });
});

describe("deleteNote", () => {
  it("issues DELETE and resolves on success", async () => {
    fetchMock.mockResolvedValue(ok(null));
    await deleteNote("n1");
    expect(fetchMock).toHaveBeenCalledWith("http://localhost:8000/notes/n1", {
      method: "DELETE",
      headers: {},
    });
  });

  it("throws on a non-ok response", async () => {
    fetchMock.mockResolvedValue(fail(404));
    await expect(deleteNote("n1")).rejects.toThrow("note delete failed: 404");
  });
});
