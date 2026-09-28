import { describe, expect, it } from "vitest";
import { isActive, NAV_ITEMS, navItemsFor, routeFor, ROUTES } from "./nav";

describe("NAV_ITEMS (route table, team contract 3.5)", () => {
  it("registers every top-level destination with its roles", () => {
    const table = Object.fromEntries(NAV_ITEMS.map((item) => [item.href, [...item.roles].sort()]));
    expect(table).toEqual({
      "/": ["coach", "parent", "player"],
      "/sessions": ["coach", "parent", "player"],
      "/progress": ["coach", "parent", "player"],
      "/reports": ["coach", "parent", "player"],
      "/wellness": ["coach", "parent", "player"],
      "/review": ["coach"],
      "/notes": ["coach", "parent"],
      "/pipeline": ["coach", "parent"],
      "/alerts": ["coach", "parent"],
      "/cameras": ["parent"],
      "/settings": ["parent"],
    });
    for (const item of NAV_ITEMS) {
      expect(item.label).not.toBe("");
      expect(item.icon).toBeDefined();
    }
  });

  it("covers the non-nav routes too", () => {
    expect(ROUTES.map((route) => route.pattern)).toEqual(
      expect.arrayContaining(["/login", "/sessions/new", "/sessions/[id]", "/pitchmap/[sessionId]"]),
    );
  });
});

describe("routeFor", () => {
  it.each([
    ["/", "/"],
    ["/login", "/login"],
    ["/sessions", "/sessions"],
    ["/sessions/", "/sessions"],
    ["/sessions/new", "/sessions/new"],
    ["/sessions/abc-123", "/sessions/[id]"],
    ["/pitchmap/s1", "/pitchmap/[sessionId]"],
    ["/cameras", "/cameras"],
  ])("maps %s to %s", (pathname, pattern) => {
    expect(routeFor(pathname)?.pattern).toBe(pattern);
  });

  it("knows the access rules", () => {
    expect(routeFor("/login")?.roles).toBe("public");
    expect(routeFor("/sessions/new")?.roles).toEqual(["parent", "coach"]);
    expect(routeFor("/sessions/x")?.roles).toEqual(["player", "parent", "coach"]);
  });

  it("returns undefined outside the table", () => {
    expect(routeFor("/nowhere")).toBeUndefined();
    expect(routeFor("/sessions/a/b")).toBeUndefined();
  });
});

describe("navItemsFor", () => {
  it("filters by role", () => {
    expect(navItemsFor("player").map((item) => item.href)).toEqual([
      "/",
      "/sessions",
      "/progress",
      "/reports",
      "/wellness",
    ]);
    expect(navItemsFor("coach").map((item) => item.href)).toContain("/review");
    expect(navItemsFor("coach").map((item) => item.href)).not.toContain("/cameras");
    expect(navItemsFor("parent").map((item) => item.href)).toContain("/settings");
  });

  it("shows everything when the role is unknown", () => {
    expect(navItemsFor(null)).toBe(NAV_ITEMS);
  });
});

describe("isActive", () => {
  it("matches home exactly and sections by prefix", () => {
    expect(isActive("/", "/")).toBe(true);
    expect(isActive("/", "/sessions")).toBe(false);
    expect(isActive("/sessions", "/sessions")).toBe(true);
    expect(isActive("/sessions", "/sessions/abc")).toBe(true);
    expect(isActive("/sessions", "/sessionsx")).toBe(false);
  });
});
