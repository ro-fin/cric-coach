"use client";

import { Suspense } from "react";

/**
 * US-K3 notes page: /notes?player_id=...&session_id=...&role=... — the panel
 * pinned to a player (and optionally one session). The role parameter only
 * shapes the UI; visibility is enforced server-side per bearer token.
 */

import { useSearchParams } from "next/navigation";
import NotesPanel from "./NotesPanel";

function NotesPageInner() {
  const params = useSearchParams();
  const playerId = params.get("player_id");
  if (playerId === null) {
    return (
      <main>
        <h1>Coach notes</h1>
        <p>Pick a player to see their notes (missing player_id).</p>
      </main>
    );
  }
  return (
    <main>
      <h1>Coach notes</h1>
      <NotesPanel
        playerId={playerId}
        sessionId={params.get("session_id") ?? undefined}
        role={params.get("role") ?? "coach"}
      />
    </main>
  );
}

/** Next.js requires a Suspense boundary around useSearchParams for prerender. */
export default function NotesPage() {
  return (
    <Suspense fallback={<main aria-busy="true" />}>
      <NotesPageInner />
    </Suspense>
  );
}
