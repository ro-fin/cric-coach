import { describe, expect, it, vi } from "vitest";
import { alert } from "@/test/fixtures.ops";
import { createAlertsApi } from "./api";

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

function client(body: unknown) {
  const fetchFn = vi.fn().mockResolvedValue(ok(body));
  return { fetchFn, api: createAlertsApi({ baseUrl: "http://lab", token: "t", fetchFn }) };
}

describe("createAlertsApi", () => {
  it("lists every visible alert by default", async () => {
    const { api, fetchFn } = client([alert()]);
    await expect(api.listAlerts()).resolves.toEqual([alert()]);
    expect(fetchFn).toHaveBeenCalledWith("http://lab/alerts", {
      method: "GET",
      headers: { Accept: "application/json", Authorization: "Bearer t" },
    });
  });

  it("passes the audience and acknowledged filters", async () => {
    const { api, fetchFn } = client([]);
    await api.listAlerts({ audience: "developer", acknowledged: false });
    expect(fetchFn.mock.calls[0][0]).toBe(
      "http://lab/alerts?audience=developer&acknowledged=false",
    );
  });

  it("acknowledges one alert", async () => {
    const acked = alert({ acknowledged: true });
    const { api, fetchFn } = client(acked);
    await expect(api.acknowledge("a1")).resolves.toEqual(acked);
    expect(fetchFn).toHaveBeenCalledWith("http://lab/alerts/a1/ack", {
      method: "POST",
      headers: { Accept: "application/json", Authorization: "Bearer t" },
    });
  });

  it("defaults to the shared lib/api configuration", () => {
    expect(typeof createAlertsApi().listAlerts).toBe("function");
  });
});
