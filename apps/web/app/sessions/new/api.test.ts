import { describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import { createNewSessionApi, detailText } from "./api";

function respond(status: number, body: unknown, text?: string): Response {
  return new Response(text ?? JSON.stringify(body), {
    status,
    statusText: status < 300 ? "OK" : "Unprocessable",
  });
}

function client(response: Response) {
  const fetchFn = vi.fn(async () => response);
  return { fetchFn, api: createNewSessionApi({ baseUrl: "/api/cricai/", token: "", fetchFn }) };
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
      headers: { Authorization: "Bearer tok", "Content-Type": "application/json" },
    });
    expect(calls[0][1]).toMatchObject({ headers: { Authorization: "Bearer tok" }, body: undefined });
  });

  it("sends no bearer through the same-origin proxy", async () => {
    const { api, fetchFn } = client(respond(200, []));
    await api.listCameras();
    expect(fetchFn).toHaveBeenCalledWith("/api/cricai/cameras", {
      method: "GET",
      headers: {},
      body: undefined,
    });
  });

  it("throws ApiError with the server's detail verbatim", async () => {
    const { api } = client(respond(409, { detail: "safety checklist not acknowledged" }));
    const failure = api.start("s1", ["C1"]);
    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({
      status: 409,
      message: "API 409: safety checklist not acknowledged",
    });
  });

  it("falls back to the status text for a non-JSON error body", async () => {
    const { api } = client(respond(502, null, "<html>bad gateway</html>"));
    await expect(api.lifecycle("s1")).rejects.toMatchObject({ message: "API 502: Unprocessable" });
  });

  it("uses the global fetch and default config when none is injected", async () => {
    const fetchFn = vi.fn(async () => respond(200, []));
    vi.stubGlobal("fetch", fetchFn);
    await createNewSessionApi().listPlayers();
    expect(fetchFn).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });
});

describe("detailText", () => {
  it("joins a structured detail's message and offending ids", () => {
    expect(
      detailText(
        {
          detail: {
            message: "cameras not registered as active in the camera registry",
            unknown_cameras: ["C9", "C10"],
          },
        },
        "x",
      ),
    ).toBe("cameras not registered as active in the camera registry — unknown_cameras: C9, C10");
    expect(
      detailText(
        { detail: { message: "all safety checklist items must be acknowledged true", missing: [], false: ["helmet_on"] } },
        "x",
      ),
    ).toBe("all safety checklist items must be acknowledged true — false: helmet_on");
  });

  it("falls back when the body carries nothing readable", () => {
    expect(detailText(undefined, "fb")).toBe("fb");
    expect(detailText({ other: 1 }, "fb")).toBe("fb");
    expect(detailText({ detail: [{ msg: "x" }] }, "fb")).toBe("fb");
    expect(detailText({ detail: { message: 3 } }, "fb")).toBe("fb");
    expect(detailText({ detail: null }, "fb")).toBe("fb");
  });
});
