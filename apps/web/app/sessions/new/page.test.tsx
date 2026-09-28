import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { RoleProvider } from "@/lib/auth/role";
import NewSessionPage from "./page";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams("player=p9"),
}));

vi.mock("./NewSessionFlow", () => ({
  default: ({ role, search }: { role: string | null; search: string }) => (
    <p>
      flow:{String(role)}:{search}
    </p>
  ),
}));

describe("NewSessionPage", () => {
  it("passes the signed-in role and the URL's search string to the flow", () => {
    render(
      <RoleProvider role="parent">
        <NewSessionPage />
      </RoleProvider>,
    );
    expect(screen.getByText("flow:parent:?player=p9")).toBeInTheDocument();
  });
});
