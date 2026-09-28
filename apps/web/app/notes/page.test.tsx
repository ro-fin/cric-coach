/** US-K3: notes page — search-param wiring for player/session/role. */

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import NotesPage from "./page";

const nav = vi.hoisted(() => ({ params: new URLSearchParams() }));

vi.mock("next/navigation", () => ({ useSearchParams: () => nav.params }));

vi.mock("./api", () => ({
  listNotes: vi.fn().mockResolvedValue([]),
  createNote: vi.fn(),
  deleteNote: vi.fn(),
}));

beforeEach(() => {
  nav.params = new URLSearchParams();
});

describe("NotesPage", () => {
  it("asks for a player when player_id is missing", () => {
    render(<NotesPage />);
    expect(screen.getByText(/missing player_id/)).toBeInTheDocument();
    expect(screen.queryByTestId("notes-panel")).not.toBeInTheDocument();
  });

  it("mounts the panel for a player with the coach default role", async () => {
    nav.params = new URLSearchParams("player_id=p1");
    render(<NotesPage />);
    expect(await screen.findByTestId("notes-panel")).toBeInTheDocument();
    expect(screen.getByRole("form", { name: "Add note" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Ball number")).not.toBeInTheDocument();
  });

  it("passes session and player role through (kid mode: read-only)", async () => {
    nav.params = new URLSearchParams("player_id=p1&session_id=s1&role=player");
    render(<NotesPage />);
    expect(await screen.findByTestId("notes-panel")).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Add note" })).not.toBeInTheDocument();
  });
});
