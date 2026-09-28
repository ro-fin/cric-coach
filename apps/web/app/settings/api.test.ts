import { describe, expect, it, vi } from "vitest";
import { appSettings } from "@/test/fixtures.ops";
import { createSettingsApi, REVIEW_MODES } from "./api";

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

function client(body: unknown) {
  const fetchFn = vi.fn().mockResolvedValue(ok(body));
  return { fetchFn, api: createSettingsApi({ baseUrl: "http://lab", token: "t", fetchFn }) };
}

describe("createSettingsApi", () => {
  it("reads the active settings and the version history", async () => {
    const { api, fetchFn } = client(appSettings());
    await expect(api.active()).resolves.toEqual(appSettings());
    await api.versions();
    expect(fetchFn.mock.calls[0][0]).toBe("http://lab/settings");
    expect(fetchFn.mock.calls[1][0]).toBe("http://lab/settings/versions");
  });

  it("appends a version with its reason", async () => {
    const { api, fetchFn } = client(appSettings({ version: 2 }));
    const body = { settings: appSettings().settings, reason: "coach gate for the season" };
    await expect(api.createVersion(body)).resolves.toMatchObject({ version: 2 });
    expect(fetchFn).toHaveBeenCalledWith("http://lab/settings", {
      method: "POST",
      headers: { Authorization: "Bearer t", "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  });

  it("defaults to the shared lib/api configuration", () => {
    expect(typeof createSettingsApi().active).toBe("function");
  });

  it("pins the two review modes", () => {
    expect(REVIEW_MODES).toEqual(["auto_publish", "coach_gate"]);
  });
});
