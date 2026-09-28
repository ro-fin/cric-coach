/** US-J5: /review page shell — heading, gate explanation, and the queue
 * mounted so the server's coach-only decision governs what renders. */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ReviewPage from "./page";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, listReviewQueue: vi.fn(async () => []), publishReport: vi.fn() };
});

describe("ReviewPage", () => {
  it("renders the heading, the gate explanation and the live queue", async () => {
    render(<ReviewPage />);
    expect(screen.getByRole("heading", { name: "Report review queue" })).toBeInTheDocument();
    expect(screen.getByText(/held for coach review/)).toBeInTheDocument();
    expect(await screen.findByTestId("review-empty")).toBeInTheDocument();
  });
});
