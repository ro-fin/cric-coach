"use client";

/**
 * US-K3 coach notes panel: timestamped free text pinned to a player, session
 * or ball — human observations beside the machine metrics.
 *
 * SAF: note bodies are UNTRUSTED input. They are rendered exclusively as
 * React text children (`{note.body}`), so any HTML/script a body carries is
 * escaped inert — covered by the red-team test. Kid mode: the API already
 * returns only `shared` notes to the player role; this panel additionally
 * hides the write controls (create/delete) from the player.
 */

import { FormEvent, useCallback, useEffect, useState } from "react";
import { createNote, deleteNote, listNotes, Note, NoteVisibility } from "./api";

export interface NotesPanelProps {
  playerId: string;
  sessionId?: string;
  role: string;
}

export default function NotesPanel({ playerId, sessionId, role }: NotesPanelProps) {
  const [notes, setNotes] = useState<Note[]>([]);
  const [search, setSearch] = useState("");
  const [body, setBody] = useState("");
  const [ballNo, setBallNo] = useState("");
  const [visibility, setVisibility] = useState<NoteVisibility>("coach_only");
  const [error, setError] = useState<string | null>(null);
  const canWrite = role !== "player";

  const refresh = useCallback(
    async (q: string) => {
      try {
        setNotes(await listNotes({ playerId, sessionId, q: q || undefined }));
        setError(null);
      } catch {
        setError("Could not load notes.");
      }
    },
    [playerId, sessionId],
  );

  useEffect(() => {
    void refresh("");
  }, [refresh]);

  async function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await refresh(search);
  }

  async function submitNote(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      await createNote({
        player_id: playerId,
        session_id: sessionId,
        ball_no: ballNo === "" ? undefined : Number(ballNo),
        body,
        visibility,
      });
      setBody("");
      setBallNo("");
      await refresh(search);
    } catch {
      setError("Could not save the note.");
    }
  }

  async function remove(id: string) {
    try {
      await deleteNote(id);
      await refresh(search);
    } catch {
      setError("Could not delete the note.");
    }
  }

  return (
    <section aria-label="Coach notes" data-testid="notes-panel">
      <h2>Coach notes</h2>
      {error !== null && <p role="alert">{error}</p>}
      <form onSubmit={submitSearch} aria-label="Search notes" className="no-print">
        <input
          type="search"
          aria-label="Search text"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <button type="submit">Search</button>
      </form>
      {canWrite && (
        <form onSubmit={submitNote} aria-label="Add note" className="no-print">
          <textarea
            aria-label="Note body"
            required
            value={body}
            onChange={(event) => setBody(event.target.value)}
          />
          {sessionId !== undefined && (
            <input
              type="number"
              min={1}
              aria-label="Ball number"
              value={ballNo}
              onChange={(event) => setBallNo(event.target.value)}
            />
          )}
          <select
            aria-label="Visibility"
            value={visibility}
            onChange={(event) => setVisibility(event.target.value as NoteVisibility)}
          >
            <option value="coach_only">Coach only</option>
            <option value="shared">Shared with player</option>
          </select>
          <button type="submit">Add note</button>
        </form>
      )}
      <ul aria-label="Notes">
        {notes.map((note) => (
          <li key={note.id} data-testid="note-item">
            <p>{note.body}</p>
            <p>
              <span data-testid="note-author">{note.author}</span>
              {" · "}
              <time dateTime={note.created_at}>{note.created_at}</time>
              {note.ball_no !== null && <span data-testid="note-ball"> · ball {note.ball_no}</span>}
              {" · "}
              <span data-testid="note-visibility">{note.visibility}</span>
            </p>
            {canWrite && (
              <button type="button" className="no-print" onClick={() => void remove(note.id)}>
                Delete
              </button>
            )}
          </li>
        ))}
      </ul>
      {notes.length === 0 && <p data-testid="notes-empty">No notes yet.</p>}
    </section>
  );
}
