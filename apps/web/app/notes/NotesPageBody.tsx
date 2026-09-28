"use client";

/**
 * US-K3 notes screen body: picks the player (the `player_id` link parameter,
 * else the first listed, with a picker to switch) and mounts the notes panel
 * with the signed-in role. What each role may read is enforced server-side.
 */

import { useCallback, useMemo, useState } from "react";
import { EmptyState, ErrorState, LinkButton, PageHeader, Skeleton } from "@/components/ui";
import { useRole } from "@/lib/auth/role";
import { createTodayApi } from "@/app/_today/api";
import type { PlayerOut } from "@/app/_today/api";
import { useLoad } from "@/app/_today/useLoad";
import { pickPlayer } from "@/app/_today/view";
import NotesPanel from "./NotesPanel";

export interface NotesPageBodyProps {
  playerIdParam: string | null;
  sessionId: string | null;
  listPlayers?: () => Promise<PlayerOut[]>;
}

export default function NotesPageBody({ playerIdParam, sessionId, listPlayers }: NotesPageBodyProps) {
  const role = useRole();
  const load = useMemo(
    () => listPlayers ?? (() => createTodayApi().listPlayers()),
    [listPlayers],
  );
  const players = useLoad(useCallback(() => load(), [load]));
  const [chosen, setChosen] = useState<string | null>(playerIdParam);

  let body;
  if (players.state.status === "loading") {
    body = <Skeleton label="Loading players" />;
  } else if (players.state.status === "error") {
    body = (
      <ErrorState
        message={`Could not load players: ${players.state.message}`}
        onRetry={players.retry}
      />
    );
  } else {
    const list = players.state.data;
    const player = pickPlayer(list, chosen);
    body =
      player === null ? (
        <EmptyState
          title="No players yet"
          action={
            <LinkButton variant="secondary" href="/settings">
              Add a player in settings
            </LinkButton>
          }
        />
      ) : (
        <div className="flex flex-col gap-5">
          {list.length > 1 && (
            <label className="flex max-w-xs flex-col gap-1 font-semibold" data-print="hide">
              Player
              <select
                className="min-h-11 rounded-lg border-2 border-border bg-surface px-3 text-ink"
                value={player.id}
                onChange={(event) => setChosen(event.target.value)}
              >
                {list.map((entry) => (
                  <option key={entry.id} value={entry.id}>
                    {entry.name}
                  </option>
                ))}
              </select>
            </label>
          )}
          <NotesPanel
            key={player.id}
            playerId={player.id}
            sessionId={sessionId ?? undefined}
            role={role}
          />
        </div>
      );
  }

  return (
    <main className="mx-auto w-full max-w-4xl p-4 md:p-6">
      <PageHeader
        title="Coach notes"
        description={
          sessionId === null
            ? "What the coach saw, beside the machine numbers."
            : `Notes pinned to session ${sessionId}.`
        }
      />
      {body}
    </main>
  );
}
