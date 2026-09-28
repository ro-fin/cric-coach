import { describe, expect, it } from "vitest";
import { LENGTHS, LINES, lineTextClass, zoneFillClass } from "./geometry";

describe("pitch-map token classes", () => {
  it("gives every length zone its zone token", () => {
    expect(LENGTHS.map(zoneFillClass)).toEqual([
      "fill-zone-yorker",
      "fill-zone-full",
      "fill-zone-good",
      "fill-zone-short",
    ]);
    expect(zoneFillClass("beamer")).toBe("fill-ink-muted");
  });

  it("gives every line channel its line token", () => {
    expect(LINES.map(lineTextClass)).toEqual([
      "text-line-outside-off",
      "text-line-off",
      "text-line-middle",
      "text-line-leg",
    ]);
    expect(lineTextClass("wide")).toBe("text-ink-muted");
  });
});
