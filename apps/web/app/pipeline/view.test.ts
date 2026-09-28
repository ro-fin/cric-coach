import { describe, expect, it } from "vitest";
import {
  canTrigger,
  formatWhen,
  newestRunId,
  PIPELINE_ROLES,
  sessionIdFromSearch,
  shortDigest,
  statusTone,
} from "./view";

describe("pipeline view model", () => {
  it("serves parents and coaches", () => {
    expect(PIPELINE_ROLES).toEqual(["parent", "coach"]);
  });

  it("maps every StageStatus to a badge tone", () => {
    expect(statusTone("pending")).toBe("neutral");
    expect(statusTone("running")).toBe("info");
    expect(statusTone("succeeded")).toBe("success");
    expect(statusTone("failed")).toBe("danger");
    expect(statusTone("skipped")).toBe("warning");
    expect(statusTone("locked")).toBe("neutral");
  });

  it("lets only the parent trigger work", () => {
    expect(canTrigger("parent")).toBe(true);
    expect(canTrigger("coach")).toBe(false);
    expect(canTrigger(null)).toBe(false);
  });

  it("reads the session from the query string", () => {
    expect(sessionIdFromSearch("?session=s1")).toBe("s1");
    expect(sessionIdFromSearch("?session=")).toBeNull();
    expect(sessionIdFromSearch("")).toBeNull();
  });

  it("opens the newest run, the last one served", () => {
    expect(newestRunId([])).toBeNull();
    expect(newestRunId([{ id: "r1" }, { id: "r2" }])).toBe("r2");
  });

  it("shortens long digests only", () => {
    expect(shortDigest(null)).toBe("none");
    expect(shortDigest("sha256:abc")).toBe("sha256:abc");
    expect(shortDigest("sha256:0123456789abcdef0123")).toBe("sha256:0123456789ab…");
  });
});

describe("formatWhen", () => {
  it("says a null timestamp is pending, with an optional word", () => {
    expect(formatWhen(null)).toBe("not finished");
    expect(formatWhen(null, "not started")).toBe("not started");
  });

  it("formats a served timestamp and keeps an unreadable one verbatim", () => {
    expect(formatWhen("2026-09-27T10:00:00Z")).toMatch(/2026/);
    expect(formatWhen("yesterday")).toBe("yesterday");
  });
});
