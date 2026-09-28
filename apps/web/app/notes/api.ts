/**
 * US-K3 notes API client. Base URL and bearer token come from the single
 * lib/api convention (NEXT_PUBLIC_API_BASE_URL + NEXT_PUBLIC_API_TOKEN; see
 * apps/web/README.md). The old localStorage token path is gone: no code ever
 * wrote it, so it could never authenticate a real device.
 *
 * Notes render machine-side: the ``body`` field is UNTRUSTED free text and is
 * only ever placed in the DOM through React's text escaping, never as HTML.
 */

import { apiBase, authHeaders } from "@/lib/api";

export type NoteVisibility = "coach_only" | "shared";

export interface Note {
  id: string;
  player_id: string;
  session_id: string | null;
  ball_no: number | null;
  body: string;
  author: string;
  visibility: string;
  created_at: string;
}

export interface NoteDraft {
  player_id: string;
  session_id?: string;
  ball_no?: number;
  body: string;
  visibility: NoteVisibility;
}

export interface NoteQuery {
  playerId: string;
  sessionId?: string;
  ballNo?: number;
  q?: string;
}

export async function listNotes(query: NoteQuery): Promise<Note[]> {
  const params = new URLSearchParams({ player_id: query.playerId });
  if (query.sessionId) params.set("session_id", query.sessionId);
  if (query.ballNo !== undefined) params.set("ball_no", String(query.ballNo));
  if (query.q) params.set("q", query.q);
  const response = await fetch(`${apiBase()}/notes?${params.toString()}`, {
    headers: authHeaders(),
  });
  if (!response.ok) throw new Error(`notes list failed: ${response.status}`);
  return (await response.json()) as Note[];
}

export async function createNote(draft: NoteDraft): Promise<Note> {
  const response = await fetch(`${apiBase()}/notes`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(draft),
  });
  if (!response.ok) throw new Error(`note create failed: ${response.status}`);
  return (await response.json()) as Note;
}

export async function deleteNote(id: string): Promise<void> {
  const response = await fetch(`${apiBase()}/notes/${id}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  if (!response.ok) throw new Error(`note delete failed: ${response.status}`);
}
