"use client";

/**
 * US-K3 coach notes panel: timestamped free text pinned to a player, session
 * or ball — human observations beside the machine metrics.
 *
 * SAF: note bodies are UNTRUSTED input. They are rendered exclusively as
 * React text children (`{note.body}`), so any HTML/script a body carries is
 * escaped inert — covered by the red-team test. Kid mode: the API already
 * returns only `shared` notes to the player role; this panel additionally
 * shows write controls (create/delete) only to parent and coach — the
 * signed-in role, never a URL parameter.
 *
 * Honest states: the list shows a skeleton while loading (never a premature
 * "No notes yet."), an error with retry, the role gate on 403, and the empty
 * state only after the server answered with nothing.
 */

import { FormEvent, useCallback, useEffect, useState } from "react";
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  ForbiddenState,
  Skeleton,
} from "@/components/ui";
import { ApiError } from "@/lib/api";
import type { Role } from "@/lib/auth/roles";
import { createNote, deleteNote, listNotes, Note, NoteVisibility } from "./api";

export interface NotesPanelProps {
  playerId: string;
  sessionId?: string;
  role: Role | null;
}

type ListState =
  | { kind: "loading" }
  | { kind: "ready"; notes: Note[] }
  | { kind: "forbidden" }
  | { kind: "error" };

const FIELD =
  "min-h-11 rounded-lg border-2 border-border bg-surface px-3 text-ink focus:border-accent";

export default function NotesPanel({ playerId, sessionId, role }: NotesPanelProps) {
  const [list, setList] = useState<ListState>({ kind: "loading" });
  const [search, setSearch] = useState("");
  const [body, setBody] = useState("");
  const [ballNo, setBallNo] = useState("");
  const [visibility, setVisibility] = useState<NoteVisibility>("coach_only");
  const [actionError, setActionError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const canWrite = role === "parent" || role === "coach";

  const refresh = useCallback(
    async (q: string) => {
      try {
        const notes = await listNotes({ playerId, sessionId, q: q || undefined });
        setList({ kind: "ready", notes });
      } catch (error) {
        setList(
          error instanceof ApiError && error.status === 403
            ? { kind: "forbidden" }
            : { kind: "error" },
        );
      }
    },
    [playerId, sessionId],
  );

  useEffect(() => {
    void refresh("");
  }, [refresh]);

  async function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setList({ kind: "loading" });
    await refresh(search);
  }

  async function submitNote(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
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
      setActionError(null);
      await refresh(search);
    } catch {
      setActionError("Could not save the note.");
    } finally {
      setSaving(false);
    }
  }

  async function remove(id: string) {
    try {
      await deleteNote(id);
      setActionError(null);
      await refresh(search);
    } catch {
      setActionError("Could not delete the note.");
    }
  }

  return (
    <section aria-label="Coach notes" data-testid="notes-panel" className="flex flex-col gap-5">
      <form
        onSubmit={submitSearch}
        aria-label="Search notes"
        className="no-print flex flex-wrap items-end gap-2"
        data-print="hide"
      >
        <input
          type="search"
          aria-label="Search text"
          placeholder="Search notes"
          className={`${FIELD} min-w-0 flex-1`}
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <Button type="submit" variant="secondary">
          Search
        </Button>
      </form>
      {canWrite && (
        <form
          onSubmit={submitNote}
          aria-label="Add note"
          className="no-print flex flex-col gap-3 rounded-xl border border-border bg-surface p-4"
          data-print="hide"
        >
          <textarea
            aria-label="Note body"
            placeholder="What did you see?"
            required
            rows={3}
            className={`${FIELD} py-2`}
            value={body}
            onChange={(event) => setBody(event.target.value)}
          />
          <div className="flex flex-wrap items-end gap-2">
            {sessionId !== undefined && (
              <input
                type="number"
                min={1}
                aria-label="Ball number"
                placeholder="Ball"
                className={`${FIELD} w-28`}
                value={ballNo}
                onChange={(event) => setBallNo(event.target.value)}
              />
            )}
            <select
              aria-label="Visibility"
              className={FIELD}
              value={visibility}
              onChange={(event) => setVisibility(event.target.value as NoteVisibility)}
            >
              <option value="coach_only">Coach only</option>
              <option value="shared">Shared with player</option>
            </select>
            <Button type="submit" variant="primary" loading={saving}>
              Add note
            </Button>
          </div>
        </form>
      )}
      {actionError !== null && <ErrorState message={actionError} />}
      {list.kind === "loading" && <Skeleton label="Loading notes" lines={3} />}
      {list.kind === "forbidden" && <ForbiddenState roles={["coach", "parent"]} />}
      {list.kind === "error" && (
        <ErrorState
          message="Could not load notes."
          onRetry={() => {
            setList({ kind: "loading" });
            void refresh(search);
          }}
        />
      )}
      {list.kind === "ready" && (
        <>
          <ul aria-label="Notes" className="flex flex-col gap-3">
            {list.notes.map((note) => (
              <li
                key={note.id}
                data-testid="note-item"
                className="flex flex-col gap-2 rounded-xl border border-border bg-surface p-4"
              >
                <p className="whitespace-pre-wrap text-lg">{note.body}</p>
                <p className="flex flex-wrap items-center gap-2 text-sm text-ink-muted">
                  <span data-testid="note-author" className="font-semibold text-ink">
                    {note.author}
                  </span>
                  {" · "}
                  <time dateTime={note.created_at}>{note.created_at}</time>
                  {note.ball_no !== null && (
                    <span data-testid="note-ball"> · ball {note.ball_no}</span>
                  )}
                  {" · "}
                  <Badge tone={note.visibility === "shared" ? "info" : "neutral"}>
                    <span data-testid="note-visibility">{note.visibility}</span>
                  </Badge>
                </p>
                {canWrite && (
                  <div className="no-print" data-print="hide">
                    <Button variant="ghost" onClick={() => void remove(note.id)}>
                      Delete
                    </Button>
                  </div>
                )}
              </li>
            ))}
          </ul>
          {list.notes.length === 0 && (
            <div data-testid="notes-empty">
              <EmptyState
                title="No notes yet."
                description={
                  canWrite
                    ? "Write what you saw at the nets; the player sees the shared ones."
                    : undefined
                }
              />
            </div>
          )}
        </>
      )}
    </section>
  );
}
