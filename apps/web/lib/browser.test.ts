import { describe, expect, it, vi } from "vitest";
import { hardNavigate } from "./browser";

describe("hardNavigate", () => {
  it("asks the browser for a full page load", () => {
    // jsdom cannot navigate and reports it on the virtual console.
    const errors = vi.spyOn(console, "error").mockImplementation(() => undefined);
    expect(() => hardNavigate("/login")).not.toThrow();
    errors.mockRestore();
  });
});
