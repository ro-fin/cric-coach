import { describe, expect, it } from "vitest";
import { isEditableTarget } from "./keyboard";

describe("isEditableTarget", () => {
  it.each(["input", "select", "textarea"])("claims key input for <%s>", (tagName) => {
    expect(isEditableTarget(document.createElement(tagName))).toBe(true);
  });

  it("leaves buttons and the body to the shortcut handlers", () => {
    expect(isEditableTarget(document.createElement("button"))).toBe(false);
    expect(isEditableTarget(document.body)).toBe(false);
  });

  it("handles null and non-element targets", () => {
    expect(isEditableTarget(null)).toBe(false);
    expect(isEditableTarget(document)).toBe(false);
  });
});
