/** US-K5: reports fetch helpers — report retrieval and export URLs on the
 * single lib/api convention (`apiBase()` + `authHeaders()`, transport-agnostic).
 * fetchReport throws a typed ApiError so the page can tell a server refusal
 * (403/404 — honest error, never the cache) from a network failure. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, apiBase, authHeaders } from "@/lib/api";
import { exportUrl, fetchReport } from "./api";

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("fetchReport", () => {
  it("fetches one report with the shared auth headers", async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "r1" }),
    } as unknown as Response);
    await expect(fetchReport("r1")).resolves.toEqual({ id: "r1" });
    expect(fetchMock).toHaveBeenCalledWith(`${apiBase()}/reports/r1`, {
      headers: authHeaders(),
    });
  });

  it("throws ApiError carrying the HTTP status on a non-ok response", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 404 } as unknown as Response);
    const error = await fetchReport("r1").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(404);
    expect((error as ApiError).message).toBe("API 404: report fetch failed");
  });
});

describe("exportUrl", () => {
  it("targets the server-side export endpoint per format on the shared base", () => {
    expect(exportUrl("r1", "pdf")).toBe(`${apiBase()}/reports/r1/export?format=pdf`);
    expect(exportUrl("r1", "png")).toBe(`${apiBase()}/reports/r1/export?format=png`);
  });
});
