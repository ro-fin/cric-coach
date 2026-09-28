import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import HomePage from "./page";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams("player=p9"),
}));

vi.mock("./_today/TodayView", () => ({
  default: ({ role, search }: { role: string | null; search: string }) => (
    <p>
      today:{String(role)}:{search}
    </p>
  ),
}));

describe("HomePage", () => {
  it("renders Today with the URL's search string", () => {
    render(<HomePage />);
    expect(screen.getByText("today:null:?player=p9")).toBeInTheDocument();
  });
});
