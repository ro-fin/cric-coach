"use client";

/**
 * US-K3 notes page: /notes?player_id=...&session_id=... — the panel pinned to
 * a player (and optionally one session). Without a player id it opens on the
 * first listed player, with a picker to switch.
 */

import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import NotesPageBody from "./NotesPageBody";

function NotesPageInner() {
  const params = useSearchParams();
  return (
    <NotesPageBody playerIdParam={params.get("player_id")} sessionId={params.get("session_id")} />
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
