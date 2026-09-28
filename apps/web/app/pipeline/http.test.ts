import { describe, expect, it, vi } from "vitest";
import { ApiError, type ApiConfig } from "@/lib/api";
import { buildUrl, detailText, request } from "./http";

function config(fetchFn: typeof fetch, token = "tok"): ApiConfig {
  return { baseUrl: "http://lab:8000/", token, fetchFn };
}

function answer(status: number, body: unknown, statusText = "Status"): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText,
    json: async () => {
      if (body instanceof Error) {
        throw body;
      }
      return body;
    },
  } as unknown as Response;
}

describe("detailText", () => {
  it("returns a string detail verbatim", () => {
    expect(detailText({ detail: "pipeline already running" }, "x")).toBe(
      "pipeline already running",
    );
  });

  it("joins a list of problems, stringifying non-string items", () => {
    expect(detailText({ detail: ["energy must be 1..5, got 9", { loc: ["body"] }] }, "x")).toBe(
      'energy must be 1..5, got 9; {"loc":["body"]}',
    );
  });

  it("leads an object detail with its message and keeps the full object", () => {
    const body = { detail: { message: "manual rows exist", blockers: ["tags"] } };
    expect(detailText(body, "x")).toBe(
      'manual rows exist {"message":"manual rows exist","blockers":["tags"]}',
    );
  });

  it("serialises an object detail without a message", () => {
    expect(detailText({ detail: { code: 7 } }, "x")).toBe('{"code":7}');
  });

  it("falls back for missing, null or non-object bodies", () => {
    expect(detailText(undefined, "Conflict")).toBe("Conflict");
    expect(detailText({ other: 1 }, "Conflict")).toBe("Conflict");
    expect(detailText({ detail: null }, "Conflict")).toBe("Conflict");
    expect(detailText({ detail: 5 }, "Conflict")).toBe("Conflict");
  });
});

describe("buildUrl", () => {
  it("strips a trailing slash and omits undefined query values", () => {
    expect(buildUrl("http://lab:8000/", "/alerts", { a: undefined, acknowledged: false })).toBe(
      "http://lab:8000/alerts?acknowledged=false",
    );
  });

  it("adds no query string when nothing is defined", () => {
    expect(buildUrl("/api/cricai", "/settings")).toBe("/api/cricai/settings");
  });
});

describe("request", () => {
  it("GETs with the bearer when a token is configured", async () => {
    const fetchFn = vi.fn().mockResolvedValue(answer(200, [1]));
    await expect(request(config(fetchFn), "/x", { query: { n: 2 } })).resolves.toEqual([1]);
    expect(fetchFn).toHaveBeenCalledWith("http://lab:8000/x?n=2", {
      method: "GET",
      headers: { Authorization: "Bearer tok" },
    });
  });

  it("sends no Authorization header for the same-origin proxy (empty token)", async () => {
    const fetchFn = vi.fn().mockResolvedValue(answer(200, {}));
    await request(config(fetchFn, ""), "/x");
    expect(fetchFn).toHaveBeenCalledWith("http://lab:8000/x", { method: "GET", headers: {} });
  });

  it("POSTs a JSON body", async () => {
    const fetchFn = vi.fn().mockResolvedValue(answer(201, { ok: 1 }));
    await request(config(fetchFn), "/x", { method: "POST", body: { a: 1 } });
    expect(fetchFn).toHaveBeenCalledWith("http://lab:8000/x", {
      method: "POST",
      headers: { Authorization: "Bearer tok", "Content-Type": "application/json" },
      body: '{"a":1}',
    });
  });

  it("throws ApiError carrying the status and the server detail", async () => {
    const fetchFn = vi.fn().mockResolvedValue(answer(403, { detail: "requires one of: parent" }));
    const failure = request(config(fetchFn), "/x");
    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({
      status: 403,
      message: "API 403: requires one of: parent",
    });
  });

  it("falls back to the status text when the error body is not JSON", async () => {
    const fetchFn = vi.fn().mockResolvedValue(answer(502, new Error("bad json"), "Bad Gateway"));
    await expect(request(config(fetchFn), "/x")).rejects.toMatchObject({
      status: 502,
      message: "API 502: Bad Gateway",
    });
  });

  it("uses the global fetch when none is injected", async () => {
    const fetchFn = vi.fn().mockResolvedValue(answer(200, "g"));
    vi.stubGlobal("fetch", fetchFn);
    await expect(request({ baseUrl: "/api", token: "" }, "/y")).resolves.toBe("g");
    expect(fetchFn).toHaveBeenCalledWith("/api/y", { method: "GET", headers: {} });
    vi.unstubAllGlobals();
  });
});
