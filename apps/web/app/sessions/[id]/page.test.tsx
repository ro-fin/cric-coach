import { describe, expect, it } from "vitest";
import type { ReactElement } from "react";
import SessionDetailClient from "./SessionDetailClient";
import SessionDetailPage from "./page";

describe("SessionDetailPage route", () => {
  it("unwraps the route param into the detail client", async () => {
    const element = (await SessionDetailPage({
      params: Promise.resolve({ id: "abc-123" }),
    })) as ReactElement<{ sessionId: string }>;
    expect(element.type).toBe(SessionDetailClient);
    expect(element.props.sessionId).toBe("abc-123");
  });
});
