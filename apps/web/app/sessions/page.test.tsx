import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import SessionsPage from "./page";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("SessionsPage route", () => {
  it("renders the session list", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ items: [], total: 0, limit: 50, offset: 0 }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      ),
    );
    render(<SessionsPage />);
    expect(screen.getByRole("heading", { name: "Sessions" })).toBeInTheDocument();
    expect(await screen.findByTestId("sessions-empty")).toBeInTheDocument();
  });
});
