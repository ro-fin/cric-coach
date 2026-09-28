import { describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import { createTodayApi } from "./api";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, statusText: status === 200 ? "OK" : "Nope" });
}

describe("createTodayApi", () => {
  it("lists players with the bearer header", async () => {
    const fetchFn = vi.fn(async () => jsonResponse([{ id: "p1" }]));
    const api = createTodayApi({ baseUrl: "http://lab:8000/", token: "t0k", fetchFn });
    await expect(api.listPlayers()).resolves.toEqual([{ id: "p1" }]);
    expect(fetchFn).toHaveBeenCalledWith("http://lab:8000/players", {
      headers: { Authorization: "Bearer t0k" },
    });
  });

  it("asks the reports router for daily reports of one player", async () => {
    const fetchFn = vi.fn(async () => jsonResponse([]));
    const api = createTodayApi({ baseUrl: "/api/cricai", token: "", fetchFn });
    await api.listDailyReports("p 1");
    expect(fetchFn).toHaveBeenCalledWith("/api/cricai/reports?player_id=p+1&kind=daily", {
      headers: {},
    });
  });

  it("reads the rolling workload summary", async () => {
    const fetchFn = vi.fn(async () => jsonResponse([]));
    const api = createTodayApi({ baseUrl: "/api/cricai", token: "", fetchFn });
    await api.workloadSummary("p/1");
    expect(fetchFn).toHaveBeenCalledWith("/api/cricai/workload/players/p%2F1/summary", {
      headers: {},
    });
  });

  it("throws ApiError carrying the status on non-2xx", async () => {
    const fetchFn = vi.fn(async () => jsonResponse({}, 403));
    const api = createTodayApi({ baseUrl: "/x", token: "", fetchFn });
    const failure = api.listPlayers();
    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({ status: 403 });
  });

  it("uses the global fetch and default config when none is injected", async () => {
    const fetchFn = vi.fn(async () => jsonResponse([]));
    vi.stubGlobal("fetch", fetchFn);
    await createTodayApi().listPlayers();
    expect(fetchFn).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });
});
