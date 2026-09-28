import { describe, expect, it, vi } from "vitest";
import { camera, healthCheck } from "@/test/fixtures.ops";
import { CAMERA_ROLES, createCamerasApi } from "./api";

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

function client(body: unknown) {
  const fetchFn = vi.fn().mockResolvedValue(ok(body));
  return { fetchFn, api: createCamerasApi({ baseUrl: "http://lab", token: "t", fetchFn }) };
}

describe("createCamerasApi", () => {
  it("lists active cameras, or history filtered by role", async () => {
    const { api, fetchFn } = client([camera()]);
    await expect(api.listCameras()).resolves.toEqual([camera()]);
    await api.listCameras({ includeHistory: true, role: "wrist" });
    expect(fetchFn.mock.calls[0][0]).toBe("http://lab/cameras");
    expect(fetchFn.mock.calls[1][0]).toBe("http://lab/cameras?include_history=true&role=wrist");
  });

  it("patches a role, including an explicit null to clear it", async () => {
    const { api, fetchFn } = client(camera("C5"));
    await api.setRole("C5", "bowling_side");
    await api.setRole("C5", null);
    expect(fetchFn).toHaveBeenNthCalledWith(1, "http://lab/cameras/C5", {
      method: "PATCH",
      headers: { Authorization: "Bearer t", "Content-Type": "application/json" },
      body: '{"role":"bowling_side"}',
    });
    expect(fetchFn.mock.calls[1][1].body).toBe('{"role":null}');
  });

  it("lists health checks, optionally for one session", async () => {
    const { api, fetchFn } = client([healthCheck()]);
    await expect(api.listHealthChecks()).resolves.toEqual([healthCheck()]);
    await api.listHealthChecks("s1");
    expect(fetchFn.mock.calls[0][0]).toBe("http://lab/health-checks");
    expect(fetchFn.mock.calls[1][0]).toBe("http://lab/health-checks?session_id=s1");
  });

  it("defaults to the shared lib/api configuration", () => {
    expect(typeof createCamerasApi().listCameras).toBe("function");
  });

  it("pins the five camera roles", () => {
    expect(CAMERA_ROLES).toEqual(["batting_side", "bowling_side", "wrist", "front_on", "other"]);
  });
});
