"use client";

/**
 * Reports index (`/reports` without a report id): one player's reports,
 * newest period first, optionally one kind. Each row opens the report
 * (`/reports?report_id=<id>`). Reviewers also see drafts and blocked reports,
 * badged; a player token only ever receives published ones (server rule).
 */

import Link from "next/link";
import { useCallback, useMemo, useState } from "react";
import {
  Badge,
  EmptyState,
  ErrorState,
  LinkButton,
  PageHeader,
  Skeleton,
} from "@/components/ui";
import type { BadgeTone } from "@/components/ui";
import { createTodayApi } from "@/app/_today/api";
import type { PlayerOut } from "@/app/_today/api";
import { useLoad } from "@/app/_today/useLoad";
import type { LoadState } from "@/app/_today/useLoad";
import { pickPlayer, playerIdFromSearch } from "@/app/_today/view";
import { listReports } from "./api";
import type { Report } from "./api";

export type KindFilter = "daily" | "weekly" | "monthly" | null;

export interface ReportsIndexApi {
  listPlayers(): Promise<PlayerOut[]>;
  listReports(playerId: string, kind: KindFilter): Promise<Report[]>;
}

const STATUS_TONE: Record<string, BadgeTone> = {
  published: "success",
  draft: "info",
  blocked: "danger",
};

export function statusTone(status: string): BadgeTone {
  return STATUS_TONE[status] ?? "neutral";
}

const KINDS: { value: KindFilter; label: string }[] = [
  { value: null, label: "All" },
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
  { value: "monthly", label: "Monthly" },
];

function failure(what: string, state: Extract<LoadState<unknown>, { status: "error" }>) {
  return state.httpStatus === 403
    ? `Your role cannot see ${what}.`
    : `Could not load ${what}: ${state.message}`;
}

function ReportList({
  api,
  player,
  kind,
}: {
  api: ReportsIndexApi;
  player: PlayerOut;
  kind: KindFilter;
}) {
  const load = useCallback(() => api.listReports(player.id, kind), [api, player.id, kind]);
  const { state, retry } = useLoad(load);
  if (state.status === "loading") return <Skeleton label="Loading reports" lines={4} />;
  if (state.status === "error") {
    return <ErrorState message={failure("reports", state)} onRetry={retry} />;
  }
  if (state.data.length === 0) {
    return (
      <EmptyState
        title="No reports yet"
        description="Reports appear after a session is analysed."
        action={
          <LinkButton variant="secondary" href="/sessions">
            See sessions
          </LinkButton>
        }
      />
    );
  }
  return (
    <ul aria-label="Reports" className="grid gap-3 md:grid-cols-2">
      {state.data.map((report) => (
        <li key={report.id} className="rounded-xl border border-border bg-surface">
          <Link
            href={`/reports?report_id=${report.id}`}
            className="flex min-h-11 flex-col gap-2 rounded-xl p-4 hover:bg-surface-raised"
          >
            <span className="text-lg font-semibold capitalize text-ink">
              {report.kind} report
            </span>
            <span className="text-ink-muted">
              {report.period_start === report.period_end
                ? report.period_start
                : `${report.period_start} to ${report.period_end}`}
            </span>
            <span>
              <Badge tone={statusTone(report.status)}>{report.status}</Badge>
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

export default function ReportsIndex({
  search,
  api: injected,
}: {
  search: string;
  api?: ReportsIndexApi;
}) {
  const api = useMemo<ReportsIndexApi>(
    () => injected ?? { listPlayers: () => createTodayApi().listPlayers(), listReports },
    [injected],
  );
  const loadPlayers = useCallback(() => api.listPlayers(), [api]);
  const players = useLoad(loadPlayers);
  const [kind, setKind] = useState<KindFilter>(null);
  const player =
    players.state.status === "ready"
      ? pickPlayer(players.state.data, playerIdFromSearch(search))
      : null;

  return (
    <main className="mx-auto w-full max-w-6xl p-4 md:p-6">
      <PageHeader
        title="Reports"
        description={player === null ? "Coaching reports, newest first." : player.name}
      />
      <div role="group" aria-label="Report kind" className="mb-6 flex flex-wrap gap-2">
        {KINDS.map((option) => (
          <button
            key={option.label}
            type="button"
            aria-pressed={kind === option.value}
            onClick={() => setKind(option.value)}
            className={`min-h-11 rounded-full border-2 px-4 font-semibold ${
              kind === option.value
                ? "border-accent bg-accent text-accent-ink"
                : "border-border bg-surface text-ink"
            }`}
          >
            {option.label}
          </button>
        ))}
      </div>
      {players.state.status === "loading" && <Skeleton label="Loading players" />}
      {players.state.status === "error" && (
        <ErrorState message={failure("players", players.state)} onRetry={players.retry} />
      )}
      {players.state.status === "ready" &&
        (player === null ? (
          <EmptyState
            title="No players yet"
            action={
              <LinkButton variant="secondary" href="/settings">
                Add a player in settings
              </LinkButton>
            }
          />
        ) : (
          <ReportList key={`${player.id}-${kind}`} api={api} player={player} kind={kind} />
        ))}
    </main>
  );
}
