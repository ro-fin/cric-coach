import { describe, expect, it, vi } from "vitest";
import { runDetail, runOut, runSummary } from "@/test/fixtures.ops";
import { createPipelineApi, STAGES } from "./api";

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

function client(body: unknown) {
  const fetchFn = vi.fn().mockResolvedValue(ok(body));
  return { fetchFn, api: createPipelineApi({ baseUrl: "http://lab", token: "t", fetchFn }) };
}

const AUTH = { Authorization: "Bearer t" };
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

  it("defaults to the shared lib/api configuration", () => {
    expect(typeof createPipelineApi().listRuns).toBe("function");
  });

  it("pins the fifteen worker stages in DAG order", () => {
    expect(STAGES).toHaveLength(15);
    expect(STAGES[0]).toBe("probe");
    expect(STAGES[14]).toBe("report");
  });
});
