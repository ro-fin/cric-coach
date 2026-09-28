import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/api";
import { accessFor, failure, LOADING, ready } from "./access";

describe("accessFor", () => {
  it("lets the API decide while the role is unknown", () => {
    expect(accessFor(null, ["parent"])).toBe("unknown");
  });

  it("allows listed roles and forbids the rest", () => {
    expect(accessFor("coach", ["parent", "coach"])).toBe("allowed");
    expect(accessFor("player", ["parent", "coach"])).toBe("forbidden");
  });
});

describe("load states", () => {
  it("builds loading and ready states", () => {
    expect(LOADING).toEqual({ kind: "loading" });
    expect(ready([1])).toEqual({ kind: "ready", data: [1] });
  });

  it("maps 401 and 403 to forbidden with the server detail", () => {
    expect(failure(new ApiError(403, "requires one of: parent"))).toEqual({
      kind: "forbidden",
      message: "API 403: requires one of: parent",
    });
    expect(failure(new ApiError(401, "invalid token")).kind).toBe("forbidden");
  });

  it("maps other failures to an error with the message", () => {
    expect(failure(new ApiError(500, "boom"))).toEqual({
      kind: "error",
      message: "API 500: boom",
    });
    expect(failure(new Error("offline"))).toEqual({ kind: "error", message: "offline" });
    expect(failure("nope")).toEqual({ kind: "error", message: "nope" });
  });
});

