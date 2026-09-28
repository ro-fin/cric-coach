/** US-K3: notes page — search-param wiring for player and session. */

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import NotesPage from "./page";

const nav = vi.hoisted(() => ({ params: new URLSearchParams() }));

vi.mock("next/navigation", () => ({ useSearchParams: () => nav.params }));

vi.mock("./NotesPageBody", () => ({
  default: ({
    playerIdParam,
    sessionId,
  }: {
    playerIdParam: string | null;
    sessionId: string | null;
  }) => (
    <p>
      notes:{String(playerIdParam)}:{String(sessionId)}
    </p>
  ),
}));

beforeEach(() => {
  nav.params = new URLSearchParams();
});

describe("NotesPage", () => {
  it("passes no player or session when the link carries none", () => {
    render(<NotesPage />);
    expect(screen.getByText("notes:null:null")).toBeInTheDocument();
  });

  it("passes the linked player and session through", () => {
    nav.params = new URLSearchParams("player_id=p1&session_id=s1");
    render(<NotesPage />);
    expect(screen.getByText("notes:p1:s1")).toBeInTheDocument();
  });
});
