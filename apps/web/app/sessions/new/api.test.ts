import { describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import { createNewSessionApi } from "./api";

function respond(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    statusText: status < 300 ? "OK" : "Unprocessable",
  });
}

describe("createNewSessionApi", () => {
  it("routes every call to its router path and method", async () => {
    const calls: [string, RequestInit][] = [];
    const fetchFn = vi.fn(async (url: string, init: RequestInit) => {
      calls.push([url, init]);
      return respond(200, {});
    }) as unknown as typeof fetch;
    const api = createNewSessionApi({ baseUrl: "http://lab:8000", token: "tok", fetchFn });
    await api.listPlayers();
    await api.createSession({
      player_id: "p1",
      date: "2026-09-28",
      session_type: "batting",
      bowler_source: "human",
      machine_settings: null,
      notes: null,
    });
    await api.getSession("s/1");
    await api.machineChecklist();
    await api.ackChecklist("s1", { items: { a: true }, acked_by: "Mum" });
    await api.listCameras();
    await api.start("s1", ["C1", "C2"]);
    await api.stop("s1", { cameras_reporting: { C2: { ok: false } } });
    await api.lifecycle("s1");
    expect(calls.map(([url, init]) => `${init.method} ${url}`)).toEqual([
      "GET http://lab:8000/players",
      "POST http://lab:8000/sessions",
      "GET http://lab:8000/sessions/s%2F1",
      "GET http://lab:8000/checklists/machine",
      "POST http://lab:8000/sessions/s1/checklist-ack",
      "GET http://lab:8000/cameras",
      "POST http://lab:8000/sessions/s1/start",
      "POST http://lab:8000/sessions/s1/stop",
      "GET http://lab:8000/sessions/s1/lifecycle",
    ]);
    expect(calls[6][1]).toMatchObject({
      body: JSON.stringify({ cameras: ["C1", "C2"] }),
      headers: expect.objectContaining({
        Authorization: "Bearer tok",
        "Content-Type": "application/json",
      }),
    });
  });

  it("keeps a structured 422 detail readable (message plus offending ids)", async () => {
    const fetchFn = vi.fn(async () =>
      respond(422, {
        detail: {
          message: "cameras not registered as active in the camera registry",
          unknown_cameras: ["C9"],
        },
      }),
    );
    const api = createNewSessionApi({ baseUrl: "/api/cricai", token: "", fetchFn });
    const failure = api.start("s1", ["C9"]);
    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({
      status: 422,
      message:
        "API 422: cameras not registered as active in the camera registry — unknown_cameras: C9",
    });
  });

  it("uses the global fetch and default config when none is injected", async () => {
    const fetchFn = vi.fn(async () => respond(200, []));
    vi.stubGlobal("fetch", fetchFn);
    await createNewSessionApi().listPlayers();
    expect(fetchFn).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });
});
