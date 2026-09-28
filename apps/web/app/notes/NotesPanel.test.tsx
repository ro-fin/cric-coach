/** US-K3: notes panel — CRUD flows, search, kid mode, and the SAF red-team
 * check that script injection in a note body renders inert (React escapes). */

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import { expectNoA11yViolations } from "@/test/axe";
import type { Note } from "./api";
import NotesPanel from "./NotesPanel";

vi.mock("./api", () => ({
  listNotes: vi.fn(),
  createNote: vi.fn(),
  deleteNote: vi.fn(),
}));

import { createNote, deleteNote, listNotes } from "./api";

const listMock = vi.mocked(listNotes);
const createMock = vi.mocked(createNote);
const deleteMock = vi.mocked(deleteNote);

function note(overrides: Partial<Note> = {}): Note {
  return {
    id: "n1",
    player_id: "p1",
    session_id: null,
    ball_no: null,
    body: "Head falling to off side.",
    author: "coach",
    visibility: "coach_only",
    created_at: "2026-07-09T10:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  listMock.mockResolvedValue([]);
});

describe("NotesPanel", () => {
  it("lists notes with author, timestamp, ball pin and visibility", async () => {
    listMock.mockResolvedValue([
      note(),
      note({ id: "n2", ball_no: 7, author: "parent", visibility: "shared" }),
    ]);
    render(<NotesPanel playerId="p1" role="coach" />);
    const items = await screen.findAllByTestId("note-item");
    expect(items).toHaveLength(2);
    expect(listMock).toHaveBeenCalledWith({ playerId: "p1", sessionId: undefined, q: undefined });
    expect(screen.getAllByTestId("note-author")[0]).toHaveTextContent("coach");
    expect(screen.getByTestId("note-ball")).toHaveTextContent("ball 7");
    expect(screen.getAllByTestId("note-visibility")[1]).toHaveTextContent("shared");
    expect(screen.queryByTestId("notes-empty")).not.toBeInTheDocument();
  });

  it("shows the empty state and load errors, and retries", async () => {
    render(<NotesPanel playerId="p1" role="coach" />);
    expect(await screen.findByTestId("notes-empty")).toBeInTheDocument();
    listMock.mockRejectedValueOnce(new Error("down"));
    fireEvent.submit(screen.getByRole("form", { name: "Search notes" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load notes.");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByTestId("notes-empty")).toBeInTheDocument();
  });

  it("never claims 'No notes yet' before the server answered", async () => {
    let release!: (notes: Note[]) => void;
    listMock.mockReturnValue(new Promise((resolve) => (release = resolve)));
    render(<NotesPanel playerId="p1" role="coach" />);
    expect(screen.getByText("Loading notes")).toBeInTheDocument();
    expect(screen.queryByTestId("notes-empty")).not.toBeInTheDocument();
    await act(async () => release([]));
    expect(screen.getByTestId("notes-empty")).toBeInTheDocument();
  });

  it("shows the role gate on 403", async () => {
    listMock.mockRejectedValue(new ApiError(403, "requires one of: ['coach', 'parent']"));
    render(<NotesPanel playerId="p1" role="player" />);
    expect(await screen.findByTestId("forbidden")).toHaveTextContent(
      "This screen is for the coach or parent.",
    );
  });

  it("gives write controls to nobody when signed out", async () => {
    render(<NotesPanel playerId="p1" role={null} />);
    await screen.findByTestId("notes-empty");
    expect(screen.queryByRole("form", { name: "Add note" })).not.toBeInTheDocument();
  });

  it("is axe clean with notes and the write form", async () => {
    listMock.mockResolvedValue([note(), note({ id: "n2", visibility: "shared", ball_no: 3 })]);
    const { container } = render(<NotesPanel playerId="p1" sessionId="s1" role="coach" />);
    await screen.findAllByTestId("note-item");
    await expectNoA11yViolations(container);
  });

  it("searches with the typed query", async () => {
    render(<NotesPanel playerId="p1" role="coach" />);
    await screen.findByTestId("notes-empty");
    fireEvent.change(screen.getByLabelText("Search text"), { target: { value: "pull" } });
    fireEvent.submit(screen.getByRole("form", { name: "Search notes" }));
    await waitFor(() =>
      expect(listMock).toHaveBeenLastCalledWith({ playerId: "p1", sessionId: undefined, q: "pull" }),
    );
  });

  it("creates a session note with ball pin and visibility, then clears the form", async () => {
    createMock.mockResolvedValue(note());
    render(<NotesPanel playerId="p1" sessionId="s1" role="coach" />);
    await screen.findByTestId("notes-empty");
    fireEvent.change(screen.getByLabelText("Note body"), { target: { value: "Nice shape." } });
    fireEvent.change(screen.getByLabelText("Ball number"), { target: { value: "7" } });
    fireEvent.change(screen.getByLabelText("Visibility"), { target: { value: "shared" } });
    fireEvent.submit(screen.getByRole("form", { name: "Add note" }));
    await waitFor(() =>
      expect(createMock).toHaveBeenCalledWith({
        player_id: "p1",
        session_id: "s1",
        ball_no: 7,
        body: "Nice shape.",
        visibility: "shared",
      }),
    );
    await waitFor(() => expect(screen.getByLabelText("Note body")).toHaveValue(""));
    expect(screen.getByLabelText("Ball number")).toHaveValue(null);
  });

  it("creates a player-level note without a ball number", async () => {
    createMock.mockResolvedValue(note());
    render(<NotesPanel playerId="p1" role="parent" />);
    await screen.findByTestId("notes-empty");
    expect(screen.queryByLabelText("Ball number")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Note body"), { target: { value: "Well played." } });
    fireEvent.submit(screen.getByRole("form", { name: "Add note" }));
    await waitFor(() =>
      expect(createMock).toHaveBeenCalledWith({
        player_id: "p1",
        session_id: undefined,
        ball_no: undefined,
        body: "Well played.",
        visibility: "coach_only",
      }),
    );
  });

  it("surfaces create failures", async () => {
    createMock.mockRejectedValue(new Error("422"));
    render(<NotesPanel playerId="p1" role="coach" />);
    await screen.findByTestId("notes-empty");
    fireEvent.change(screen.getByLabelText("Note body"), { target: { value: "x" } });
    fireEvent.submit(screen.getByRole("form", { name: "Add note" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not save the note.");
  });

  it("deletes a note and refreshes", async () => {
    listMock.mockResolvedValueOnce([note()]).mockResolvedValueOnce([]);
    deleteMock.mockResolvedValue(undefined);
    render(<NotesPanel playerId="p1" role="coach" />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    await waitFor(() => expect(deleteMock).toHaveBeenCalledWith("n1"));
    expect(await screen.findByTestId("notes-empty")).toBeInTheDocument();
  });

  it("surfaces delete failures", async () => {
    listMock.mockResolvedValue([note()]);
    deleteMock.mockRejectedValue(new Error("500"));
    render(<NotesPanel playerId="p1" role="coach" />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not delete the note.");
  });

  it("kid mode (SAF): the player role gets no write controls", async () => {
    listMock.mockResolvedValue([note({ visibility: "shared" })]);
    render(<NotesPanel playerId="p1" sessionId="s1" role="player" />);
    await screen.findByTestId("note-item");
    expect(screen.queryByRole("form", { name: "Add note" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
  });

  it("renders script injection in a note body inert (SAF red team)", async () => {
    const payload = '<script>alert("pwn")</script><img src=x onerror="alert(1)">';
    listMock.mockResolvedValue([note({ body: payload })]);
    const { container } = render(<NotesPanel playerId="p1" role="coach" />);
    await screen.findByTestId("note-item");
    // React escapes text children: the payload is visible as text, and no
    // script or img element ever enters the DOM.
    expect(screen.getByText(payload)).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
  });
});
