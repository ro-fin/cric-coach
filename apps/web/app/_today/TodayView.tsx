"use client";

/**
 * Today (home): the one screen the player opens before a net session.
 *
 * Shows the newest PUBLISHED daily report — one correction, one drill, one
 * goal, and the evidence clips behind the correction (each opens the session
 * player at that ball) — next to this week's bowling workload against the
 * age-band ceiling. Parent and coach also get "Start session". Every number
 * and sentence is the API's, rendered verbatim (US-K4); each panel loads on
 * its own and shows loading, empty, error and degraded states honestly.
 */

import { ShieldAlert } from "lucide-react";
import Link from "next/link";
import { useCallback, useMemo, type ReactNode } from "react";
import {
  Badge,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  DegradedBanner,
  EmptyState,
  ErrorState,
  PageHeader,
  Skeleton,
  StatTile,
} from "@/components/ui";
import type { Role } from "@/lib/auth/role";
import { metricLabel } from "@/app/reports/labels";
import { createTodayApi } from "./api";
import type { PlayerOut, ReportOut, TodayApi, WindowSummaryOut } from "./api";
import { useLoad } from "./useLoad";
import type { LoadState } from "./useLoad";
import {
  canStartSession,
  evidenceLinks,
  latestPublished,
  pickPlayer,
  playerIdFromSearch,
  reportCaveats,
  safetyLabel,
  workloadTone,
} from "./view";

export interface TodayViewProps {
  role: Role | null;
  search: string;
  api?: TodayApi;
}

/** A link that looks like a primary button (no link variant in `Button`). */
const PRIMARY_LINK =
  "inline-flex min-h-11 items-center justify-center rounded-lg border-2 border-accent bg-accent px-4 font-semibold text-accent-ink hover:opacity-90";
const TEXT_LINK = "inline-flex min-h-11 items-center font-semibold text-accent underline";

const NO_CEILING = "No ceiling set for this age band";

function failureText(what: string, state: { message: string; httpStatus: number | null }) {
  return state.httpStatus === 403
    ? `Your role cannot see ${what}.`
    : `Could not load ${what}: ${state.message}`;
}

function Panel<T>({
  title,
  what,
  load,
  children,
}: {
  title: string;
  what: string;
  load: { state: LoadState<T>; retry: () => void };
  children: (data: T) => ReactNode;
}) {
  const { state, retry } = load;
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardBody>
        {state.status === "loading" && <Skeleton label={`Loading ${what}`} />}
        {state.status === "error" && (
          <ErrorState message={failureText(what, state)} onRetry={retry} />
        )}
        {state.status === "ready" && children(state.data)}
      </CardBody>
    </Card>
  );
}

function ReportContent({ report }: { report: ReportOut }) {
  const { body } = report;
  const safety = body.safety;
  return (
    <div className="flex flex-col gap-5">
      <p className="text-ink-muted">Report for {report.period_start}</p>
      {safety != null && safety.active && (
        <p
          role="alert"
          className="flex items-center gap-2 rounded-xl border-2 border-danger px-4 py-3 font-semibold text-danger"
        >
          <ShieldAlert aria-hidden="true" className="size-6 shrink-0" />
          {safety.text}
        </p>
      )}
      <DegradedBanner reasons={reportCaveats(report)} />
      <article aria-labelledby="today-correction">
        <h3 id="today-correction" className="text-lg font-semibold">
          One correction
        </h3>
        {body.main_correction == null ? (
          <p className="text-ink-muted">No correction today.</p>
        ) : (
          <>
            <p className="text-lg">{body.main_correction.text}</p>
            <ul aria-label="Evidence clips" className="mt-2 flex flex-wrap gap-2">
              {evidenceLinks(body.main_correction.evidence, report.session_id).map((clip) => (
                <li key={clip.key}>
                  {clip.href === null ? (
                    <Badge tone="neutral">{clip.label}</Badge>
                  ) : (
                    <Link
                      href={clip.href}
                      className="inline-flex min-h-11 items-center rounded-lg border-2 border-border bg-surface-raised px-3 font-semibold text-ink"
                    >
                      {clip.label}
                    </Link>
                  )}
                </li>
              ))}
            </ul>
          </>
        )}
      </article>
      <article aria-labelledby="today-drill">
        <h3 id="today-drill" className="text-lg font-semibold">
          One drill
        </h3>
        <p className={body.drill == null ? "text-ink-muted" : "text-lg"}>
          {body.drill == null ? "No drill today." : body.drill.text}
        </p>
      </article>
      <article aria-labelledby="today-goal">
        <h3 id="today-goal" className="text-lg font-semibold">
          One goal
        </h3>
        {body.goal == null ? (
          <p className="text-ink-muted">No goal today.</p>
        ) : (
          <StatTile
            label={metricLabel(body.goal.metric)}
            value={body.goal.target === null ? null : String(body.goal.target)}
            reason={body.goal.target === null ? "No target set" : null}
          />
        )}
      </article>
      {body.positive ? <p className="font-semibold text-success">{body.positive}</p> : null}
      <Link href={`/reports?report_id=${report.id}`} className={TEXT_LINK}>
        Open the full report
      </Link>
    </div>
  );
}

function WorkloadContent({ window }: { window: WindowSummaryOut }) {
  const ceiling = window.ceiling_overs;
  return (
    <div data-tone={workloadTone(window)} className="flex flex-col gap-4">
      <p className="text-ink-muted">
        {window.window_start} to {window.window_end}
      </p>
      <div className="grid gap-3 sm:grid-cols-2">
        <StatTile
          label="Overs bowled"
          value={String(window.weighted_overs)}
          unit={ceiling === null ? undefined : `of ${ceiling}`}
          reason={ceiling === null ? NO_CEILING : null}
        />
        <StatTile
          label="Balls left this week"
          value={window.remaining_balls === null ? null : String(window.remaining_balls)}
          reason={window.remaining_balls === null ? NO_CEILING : null}
        />
      </div>
      {window.violations.length === 0 ? (
        <Badge tone="success">Within the safe workload</Badge>
      ) : (
        <ul aria-label="Workload warnings" className="flex flex-wrap gap-2">
          {window.violations.map((code) => (
            <li key={code}>
              <Badge tone="danger">{safetyLabel(code)}</Badge>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function PlayerToday({ api, player }: { api: TodayApi; player: PlayerOut }) {
  const loadReports = useCallback(() => api.listDailyReports(player.id), [api, player.id]);
  const loadWorkload = useCallback(() => api.workloadSummary(player.id), [api, player.id]);
  const reports = useLoad(loadReports);
  const workload = useLoad(loadWorkload);
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Panel title="Today's report" what="today's report" load={reports}>
        {(list) => {
          const report = latestPublished(list);
          return report === null ? (
            <EmptyState
              title="No published report yet"
              description="A report appears here once a session is analysed and published."
              action={
                <Link href="/sessions" className={TEXT_LINK}>
                  See sessions
                </Link>
              }
            />
          ) : (
            <ReportContent report={report} />
          );
        }}
      </Panel>
      <Panel title="Workload" what="the workload" load={workload}>
        {(windows) =>
          windows.length === 0 ? (
            <EmptyState
              title="No workload window returned"
              action={
                <Link href="/wellness" className={TEXT_LINK}>
                  Open wellness
                </Link>
              }
            />
          ) : (
            <WorkloadContent window={windows[0]} />
          )
        }
      </Panel>
    </div>
  );
}

export default function TodayView({ role, search, api: injected }: TodayViewProps) {
  const api = useMemo(() => injected ?? createTodayApi(), [injected]);
  const loadPlayers = useCallback(() => api.listPlayers(), [api]);
  const players = useLoad(loadPlayers);
  const player =
    players.state.status === "ready"
      ? pickPlayer(players.state.data, playerIdFromSearch(search))
      : null;

  return (
    <main className="mx-auto w-full max-w-6xl p-4 md:p-6">
      <PageHeader
        title="Today"
        description={player === null ? undefined : player.name}
        actions={
          canStartSession(role) ? (
            <Link
              href={player === null ? "/sessions/new" : `/sessions/new?player=${player.id}`}
              className={PRIMARY_LINK}
            >
              Start session
            </Link>
          ) : undefined
        }
      />
      {players.state.status === "loading" && <Skeleton label="Loading players" />}
      {players.state.status === "error" && (
        <ErrorState message={failureText("players", players.state)} onRetry={players.retry} />
      )}
      {players.state.status === "ready" &&
        (player === null ? (
          <EmptyState
            title="No players yet"
            action={
              <Link href="/settings" className={TEXT_LINK}>
                Add a player in settings
              </Link>
            }
          />
        ) : (
          <PlayerToday key={player.id} api={api} player={player} />
        ))}
    </main>
  );
}
