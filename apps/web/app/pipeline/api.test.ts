import { describe, expect, it, vi } from "vitest";
import { runDetail, runOut, runSummary } from "@/test/fixtures.ops";
import { blockedDetail, createPipelineApi, STAGES } from "./api";

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

function client(body: unknown) {
  const fetchFn = vi.fn().mockResolvedValue(ok(body));
  return { fetchFn, api: createPipelineApi({ baseUrl: "http://lab", token: "t", fetchFn }) };
}

const AUTH = { Accept: "application/json", Authorization: "Bearer t" };
const JSON_AUTH = { ...AUTH, "Content-Type": "application/json" };

describe("createPipelineApi", () => {
  it("lists a session's runs", async () => {
    const { api, fetchFn } = client([runSummary()]);
    await expect(api.listRuns("s1")).resolves.toEqual([runSummary()]);
    expect(fetchFn).toHaveBeenCalledWith("http://lab/pipeline/sessions/s1/runs", {
      method: "GET",
      headers: AUTH,
    });
  });

  it("reads one run's full trace", async () => {
    const { api, fetchFn } = client(runDetail());
    await expect(api.runDetail("r1")).resolves.toEqual(runDetail());
    expect(fetchFn).toHaveBeenCalledWith("http://lab/pipeline/runs/r1", {
      method: "GET",
      headers: AUTH,
    });
  });

  it("triggers and resumes with an empty TriggerIn by default", async () => {
    const { api, fetchFn } = client(runOut());
    await api.triggerRun("s1");
    await api.resumeRun("s1", { detector_context: { v: 2 } });
    expect(fetchFn).toHaveBeenNthCalledWith(1, "http://lab/pipeline/sessions/s1/runs", {
      method: "POST",
      headers: JSON_AUTH,
      body: "{}",
    });
    expect(fetchFn).toHaveBeenNthCalledWith(2, "http://lab/pipeline/sessions/s1/resume", {
      method: "POST",
      headers: JSON_AUTH,
      body: '{"detector_context":{"v":2}}',
    });
  });

  it("re-derives with defaults or explicit confirmation", async () => {
    const { api, fetchFn } = client({});
    await api.rederive("s1");
    await api.rederive("s1", { confirm_manual_invalidation: true });
    expect(fetchFn).toHaveBeenNthCalledWith(1, "http://lab/pipeline/sessions/s1/rederive", {
      method: "POST",
      headers: JSON_AUTH,
      body: "{}",
    });
    expect(fetchFn.mock.calls[1][1].body).toBe('{"confirm_manual_invalidation":true}');
  });

  function answering(status: number, body: unknown, statusText = "x") {
    const fetchFn = vi.fn().mockResolvedValue({
      ok: status < 300,
      status,
      statusText,
      json: async () => {
        if (body instanceof Error) {
          throw body;
        }
        return body;
      },
    });
    return createPipelineApi({ baseUrl: "http://lab/", token: "", fetchFn });
  }

  it("returns the cascade outcome, without a bearer in proxy mode", async () => {
    const out = { session_id: "s1", counts: {}, manual_invalidation: false, run: null, run_locked: true };
    const fetchFn = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => out });
    const api = createPipelineApi({ baseUrl: "/api/cricai", token: "", fetchFn });
    await expect(api.rederive("s1")).resolves.toEqual({ kind: "done", out });
    expect(fetchFn.mock.calls[0][1].headers).toEqual({
      Accept: "application/json",
      "Content-Type": "application/json",
    });
  });

  it("turns the structured 409 into a blocked decision", async () => {
    const detail = { message: "re-derive blocked", blockers: { ball_tags: 24 } };
    await expect(answering(409, { detail }).rederive("s1")).resolves.toEqual({
      kind: "blocked",
      message: "re-derive blocked",
      blockers: { ball_tags: 24 },
    });
  });

  it("throws other failures with the server detail", async () => {
    await expect(answering(409, { detail: "session s1 is busy" }).rederive("s1")).rejects.toMatchObject({
      status: 409,
      message: "API 409: session s1 is busy",
    });
    await expect(answering(403, { detail: { message: "no" } }).rederive("s1")).rejects.toMatchObject({
      status: 403,
    });
    await expect(answering(502, new Error("html"), "Bad Gateway").rederive("s1")).rejects.toMatchObject({
      message: "API 502: Bad Gateway",
    });
  });

  it("reads only well-formed blocked bodies", () => {
    expect(blockedDetail(null)).toBeNull();
    expect(blockedDetail({ detail: "text" })).toBeNull();
    expect(blockedDetail({ detail: { message: 1, blockers: {} } })).toBeNull();
    expect(blockedDetail({ detail: { message: "m", blockers: null } })).toBeNull();
    expect(blockedDetail({ detail: { message: "m", blockers: { a: 1 } } })).toEqual({
      message: "m",
      blockers: { a: 1 },
    });
  });

  it("defaults to the shared lib/api configuration", () => {
    expect(typeof createPipelineApi().listRuns).toBe("function");
  });

  it("pins the fifteen worker stages in DAG order", () => {
    expect(STAGES).toHaveLength(15);
    expect(STAGES[0]).toBe("probe");
    expect(STAGES[14]).toBe("report");
  });
});

describe("rederive default fetch", () => {
  it("uses the global fetch when none is injected", async () => {
    const out = { session_id: "s1", counts: {}, manual_invalidation: false, run: null, run_locked: false };
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => out });
    vi.stubGlobal("fetch", fetchMock);
    await expect(createPipelineApi({ baseUrl: "/api/cricai", token: "" }).rederive("s1")).resolves.toEqual({
      kind: "done",
      out,
    });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/cricai/pipeline/sessions/s1/rederive");
    vi.unstubAllGlobals();
  });
});
