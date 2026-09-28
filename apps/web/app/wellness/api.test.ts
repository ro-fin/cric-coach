import { describe, expect, it, vi } from "vitest";
import { checkin, clearance, wellnessState } from "@/test/fixtures.ops";
import { createWellnessApi, SORENESS_BODY_KEYS } from "./api";

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

function client(body: unknown) {
  const fetchFn = vi.fn().mockResolvedValue(ok(body));
  return { fetchFn, api: createWellnessApi({ baseUrl: "http://lab", token: "t", fetchFn }) };
}

const AUTH = { Authorization: "Bearer t" };
const JSON_AUTH = { ...AUTH, "Content-Type": "application/json" };

describe("createWellnessApi", () => {
  it("lists check-ins with and without a date range", async () => {
    const { api, fetchFn } = client([checkin()]);
    await expect(api.listCheckins("p1")).resolves.toEqual([checkin()]);
    await api.listCheckins("p1", { start: "2026-09-01", end: "2026-09-28" });
    expect(fetchFn).toHaveBeenNthCalledWith(1, "http://lab/wellness/p1/checkins", {
      method: "GET",
      headers: AUTH,
    });
    expect(fetchFn.mock.calls[1][0]).toBe(
      "http://lab/wellness/p1/checkins?start=2026-09-01&end=2026-09-28",
    );
  });

  it("posts a check-in body verbatim", async () => {
    const { api, fetchFn } = client(checkin());
    const body = { checkin_date: "2026-09-28", energy: 3, pain: true, pain_note: "elbow" };
    await api.createCheckin("p1", body);
    expect(fetchFn).toHaveBeenCalledWith("http://lab/wellness/p1/checkins", {
      method: "POST",
      headers: JSON_AUTH,
      body: JSON.stringify(body),
    });
  });

  it("posts an adult clearance note", async () => {
    const { api, fetchFn } = client(clearance());
    await expect(api.clearPain("p1", "c1", "rested")).resolves.toEqual(clearance());
    expect(fetchFn).toHaveBeenCalledWith("http://lab/wellness/p1/checkins/c1/clearance", {
      method: "POST",
      headers: JSON_AUTH,
      body: '{"note":"rested"}',
    });
  });

  it("reads the state, optionally as of a date", async () => {
    const { api, fetchFn } = client(wellnessState());
    await expect(api.state("p1")).resolves.toEqual(wellnessState());
    await api.state("p1", "2026-09-01");
    expect(fetchFn.mock.calls[0][0]).toBe("http://lab/wellness/p1/state");
    expect(fetchFn.mock.calls[1][0]).toBe("http://lab/wellness/p1/state?as_of=2026-09-01");
  });

  it("defaults to the shared lib/api configuration", () => {
    expect(typeof createWellnessApi().state).toBe("function");
  });

  it("pins the thirty server soreness keys", () => {
    expect(SORENESS_BODY_KEYS).toHaveLength(30);
    expect(new Set(SORENESS_BODY_KEYS).size).toBe(30);
  });
});
